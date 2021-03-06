# Copyright 2016 AC Technologies LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Binary for training translation models and decoding from them.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import os
import re
import numpy as np
import six
import sys

from tensor2tensor.data_generators.problem import problem_hparams_to_features
import tensorflow as tf
from tensorflow.python.estimator import estimator as estimator_lib
from tensorflow.python.framework import graph_util
from tensorflow.python.util import compat

# Dependency imports

from tensor2tensor import models # pylint: disable=unused-import

from g2p_seq2seq import g2p_problem
from g2p_seq2seq import g2p_trainer_utils
from tensor2tensor.utils import registry
from tensor2tensor.utils import usr_dir
from tensor2tensor.utils import decoding
from tensor2tensor.utils import trainer_lib

from tensor2tensor.data_generators import text_encoder
from six.moves import input
from six import text_type

EOS = text_encoder.EOS


class G2PModel(object):
  """Grapheme-to-Phoneme translation model class.
  """
  def __init__(self, params, train_path="", dev_path="", test_path="",
               cleanup=False, p2g_mode=False):
    # Point out the current directory with t2t problem specified for g2p task.
    usr_dir.import_usr_dir(os.path.dirname(os.path.abspath(__file__)))
    self.params = params
    self.test_path = test_path
    if not os.path.exists(self.params.model_dir):
      os.makedirs(self.params.model_dir)

    # Register g2p problem.
    self.problem = registry._PROBLEMS[self.params.problem_name](
        self.params.model_dir, train_path=train_path, dev_path=dev_path,
        test_path=test_path, cleanup=cleanup, p2g_mode=p2g_mode)

    self.frozen_graph_filename = os.path.join(self.params.model_dir,
                                              "frozen_model.pb")
    self.inputs, self.features, self.input_fn = None, None, None
    self.mon_sess, self.estimator_spec, self.g2p_gt_map = None, None, None
    self.first_ex = False
    if train_path:
      self.train_preprocess_file_path, self.dev_preprocess_file_path =\
          None, None
      self.estimator, self.decode_hp, self.hparams =\
          self.__prepare_model(train_mode=True)
      self.train_preprocess_file_path, self.dev_preprocess_file_path =\
          self.problem.generate_preprocess_data()

    elif os.path.exists(self.frozen_graph_filename):
      self.estimator, self.decode_hp, self.hparams =\
          self.__prepare_model()
      self.__load_graph()
      self.checkpoint_path = tf.train.latest_checkpoint(self.params.model_dir)

    else:
      self.estimator, self.decode_hp, self.hparams =\
          self.__prepare_model()

  def __prepare_model(self, train_mode=False):
    """Prepare utilities for decoding."""
    hparams = registry.hparams(self.params.hparams_set)
    hparams.problem = self.problem
    hparams.problem_hparams = self.problem.get_hparams(hparams)
    if self.params.hparams:
      tf.logging.info("Overriding hparams in %s with %s",
                      self.params.hparams_set,
                      self.params.hparams)
      hparams = hparams.parse(self.params.hparams)
    trainer_run_config = g2p_trainer_utils.create_run_config(hparams,
        self.params)
    if train_mode:
      exp_fn = g2p_trainer_utils.create_experiment_fn(self.params, self.problem)
      self.exp = exp_fn(trainer_run_config, hparams)

    decode_hp = decoding.decode_hparams(self.params.decode_hparams)
    estimator = trainer_lib.create_estimator(
        self.params.model_name,
        hparams,
        trainer_run_config,
        decode_hparams=decode_hp,
        use_tpu=False)

    return estimator, decode_hp, hparams

  def __prepare_interactive_model(self):
    """Create monitored session and generator that reads from the terminal and
    yields "interactive inputs".

    Due to temporary limitations in tf.learn, if we don't want to reload the
    whole graph, then we are stuck encoding all of the input as one fixed-size
    numpy array.

    We yield int32 arrays with shape [const_array_size].  The format is:
    [num_samples, decode_length, len(input ids), <input ids>, <padding>]

    Raises:
      ValueError: Could not find a trained model in model_dir.
      ValueError: if batch length of predictions are not same.
    """

    def input_fn():
      """Input function returning features which is a dictionary of
        string feature name to `Tensor` or `SparseTensor`. If it returns a
        tuple, first item is extracted as features. Prediction continues until
        `input_fn` raises an end-of-input exception (`OutOfRangeError` or
        `StopIteration`)."""
      gen_fn = decoding.make_input_fn_from_generator(
          self.__interactive_input_fn())
      example = gen_fn()
      example = decoding._interactive_input_tensor_to_features_dict(
          example, self.hparams)
      return example

    self.res_iter = self.estimator.predict(input_fn)

    if os.path.exists(self.frozen_graph_filename):
      return

    # List of `SessionRunHook` subclass instances. Used for callbacks inside
    # the prediction call.
    hooks = estimator_lib._check_hooks_type(None)

    # Check that model has been trained.
    # Path of a specific checkpoint to predict. The latest checkpoint
    # in `model_dir` is used
    checkpoint_path = estimator_lib.saver.latest_checkpoint(
        self.params.model_dir)
    if not checkpoint_path:
      raise ValueError('Could not find trained model in model_dir: {}.'
                       .format(self.params.model_dir))

    with estimator_lib.ops.Graph().as_default() as graph:

      estimator_lib.random_seed.set_random_seed(
          self.estimator._config.tf_random_seed)
      self.estimator._create_and_assert_global_step(graph)

      self.features, input_hooks = self.estimator._get_features_from_input_fn(
          input_fn, estimator_lib.model_fn_lib.ModeKeys.PREDICT)
      self.estimator_spec = self.estimator._call_model_fn(
          self.features, None, estimator_lib.model_fn_lib.ModeKeys.PREDICT,
          self.estimator.config)
      try:
        self.mon_sess = estimator_lib.training.MonitoredSession(
            session_creator=estimator_lib.training.ChiefSessionCreator(
                checkpoint_filename_with_path=checkpoint_path,
                scaffold=self.estimator_spec.scaffold,
                config=self.estimator._session_config),
            hooks=hooks)
      except:
        # raise StandardError("Invalid model in {}".format(self.params.model_dir))
        raise ValueError("Invalid model in {}".format(self.params.model_dir))

  def decode_word(self, word):
    """Decode word.

    Args:
      word: word for decoding.

    Returns:
      pronunciation: a decoded phonemes sequence for input word.
    """
    num_samples = 1
    decode_length = 100
    vocabulary = self.problem.source_vocab
    # This should be longer than the longest input.
    const_array_size = 10000

    input_ids = vocabulary.encode(word)
    input_ids.append(text_encoder.EOS_ID)
    self.inputs = [num_samples, decode_length, len(input_ids)] + input_ids
    assert len(self.inputs) < const_array_size
    self.inputs += [0] * (const_array_size - len(self.inputs))

    result = next(self.res_iter)
    pronunciations = []
    if self.decode_hp.return_beams:
      beams = np.split(result["outputs"], self.decode_hp.beam_size, axis=0)
      for k, beam in enumerate(beams):
        tf.logging.info("BEAM %d:" % k)
        beam_string = self.problem.target_vocab.decode(
            decoding._save_until_eos(beam, is_image=False))
        pronunciations.append(beam_string)
        tf.logging.info(beam_string)
    else:
      if self.decode_hp.identity_output:
        tf.logging.info(" ".join(map(str, result["outputs"].flatten())))
      else:
        res = result["outputs"].flatten()
        if text_encoder.EOS_ID in res:
          index = list(res).index(text_encoder.EOS_ID)
          res = res[0:index]
        pronunciations.append(self.problem.target_vocab.decode(res))
    return pronunciations

  def __interactive_input_fn(self):
    num_samples = self.decode_hp.num_samples if self.decode_hp.num_samples > 0\
        else 1
    decode_length = self.decode_hp.extra_length
    input_type = "text"
    p_hparams = self.hparams.problem_hparams
    has_input = "inputs" in p_hparams.input_modality
    vocabulary = p_hparams.vocabulary["inputs" if has_input else "targets"]
    # Import readline if available for command line editing and recall.
    try:
      import readline  # pylint: disable=g-import-not-at-top,unused-variable
    except ImportError:
      pass
    while True:
      features = {
          "inputs": np.array(self.inputs).astype(np.int32),
      }
      for k, v in six.iteritems(problem_hparams_to_features(p_hparams)):
        features[k] = np.array(v).astype(np.int32)
      yield features

  def __run_op(self, sess, decode_op, feed_input):
    """Run tensorflow operation for decoding."""
    results = sess.run(decode_op,
                       feed_dict={"inp_decode:0" : [feed_input]})
    return results

  def train(self):
    """Run training."""
    print('Training started.')
    execute_schedule(self.exp, self.params)

  def interactive(self):
    """Interactive decoding."""
    self.inputs = []
    self.__prepare_interactive_model()

    if os.path.exists(self.frozen_graph_filename):
      with tf.Session(graph=self.graph) as sess:
        saver = tf.train.import_meta_graph(self.checkpoint_path + ".meta",
                                           import_scope=None,
                                           clear_devices=True)
        saver.restore(sess, self.checkpoint_path)
        inp = tf.placeholder(tf.string, name="inp_decode")[0]
        decode_op = tf.py_func(self.decode_word, [inp], tf.string)
        while True:
          word = get_word()
          pronunciations = self.__run_op(sess, decode_op, word)
          print (" ".join(pronunciations))
    else:
      while not self.mon_sess.should_stop():
        word = get_word()
        pronunciations = self.decode_word(word)
        print(" ".join(pronunciations))
        # To make sure the output buffer always flush at this level
        sys.stdout.flush()

  def decode(self, output_file_path):
    """Run decoding mode."""
    outfile = None
    # Output results to a file if given.
    if output_file_path:
      tf.logging.info("Writing decodes into %s" % output_file_path)
      outfile = tf.gfile.Open(output_file_path, "w")

    if os.path.exists(self.frozen_graph_filename):
      with tf.Session(graph=self.graph) as sess:
        inp = tf.placeholder(tf.string, name="inp_decode")[0]
        decode_op = tf.py_func(self.__decode_from_file, [inp],
                               [tf.string, tf.string])
        [inputs, decodes] = self.__run_op(sess, decode_op, self.test_path)
    else:
      inputs, decodes = self.__decode_from_file(self.test_path)
    
    # Output decoding results
    for _input, _decode in zip(inputs, decodes):
      _input = compat.as_text(_input)
      _decode = compat.as_text(_decode)
      if output_file_path:
        outfile.write("{} {}\n".format(_input, _decode))
      else:
        print("Raw prediction: {} {}".format(_input, _decode))

  def evaluate(self):
    """Run evaluation mode."""
    words, pronunciations = [], []
    for case in self.problem.generator(self.test_path,
                                       self.problem.source_vocab,
                                       self.problem.target_vocab):
      word = self.problem.source_vocab.decode(case["inputs"]).replace(
          EOS, "").strip()
      pronunciation = self.problem.target_vocab.decode(case["targets"]).replace(
          EOS, "").strip()
      words.append(word)
      pronunciations.append(pronunciation)

    self.g2p_gt_map = create_g2p_gt_map(words, pronunciations)

    if os.path.exists(self.frozen_graph_filename):
      with tf.Session(graph=self.graph) as sess:
        inp = tf.placeholder(tf.string, name="inp_decode")[0]
        decode_op = tf.py_func(self.calc_errors, [inp], 
                              [tf.int64, tf.int64, tf.int64, tf.int64])
        results = self.__run_op(sess, decode_op, self.test_path)

    else:
      results = self.calc_errors(self.test_path)
    
    word_correct, word_errors, phone_errors, total_ref_phones = results
    wer = 100.0 * word_errors / (word_correct + word_errors)
    per = 100.0 * phone_errors / total_ref_phones

    print("="*80)
    print("Total: {} words, {} phones".\
          format(word_correct + word_errors, total_ref_phones))
    print("Word errors: {} ({:.2f}%)".format(word_errors, wer))
    print("Phone errors: {} ({:.2f}%)".format(phone_errors, per))
    print("Total word errors: {}".format(word_errors))
    print("Total phone errors: {}".format(phone_errors))
    print("="*80)

  def freeze(self):
    """Freeze pre-trained model."""
    # We retrieve our checkpoint fullpath
    checkpoint = tf.train.get_checkpoint_state(self.params.model_dir)
    input_checkpoint = checkpoint.model_checkpoint_path

    # We precise the file fullname of our freezed graph
    absolute_model_folder = "/".join(input_checkpoint.split('/')[:-1])
    output_graph = absolute_model_folder + "/frozen_model.pb"

    # Before exporting our graph, we need to precise what is our output node
    # This is how TF decides what part of the Graph he has to keep and what
    # part it can dump
    # NOTE: this variable is plural, because you can have multiple output nodes
    output_node_names = []
    hparams = self.params.hparams.split(",")
    num_layers = [int(hp.split("=")[1]) for hp in hparams 
                  if hp.startswith("num_hidden_layers")][0]
    root_dir = "transformer/parallel_0_4/transformer/transformer/body"
    for i in range(num_layers):
      output_node_names.append("{}/encoder/layer_{}/self_attention/".format(root_dir, i) +\
          "multihead_attention/dot_product_attention/attention_weights")

    for i in range(num_layers):
      output_node_names.append("{}/decoder/layer_{}/self_attention/".format(root_dir, i) +\
          "multihead_attention/dot_product_attention/attention_weights")
      output_node_names.append("{}/decoder/layer_{}/encdec_attention/".format(root_dir, i) +\
          "multihead_attention/dot_product_attention/attention_weights")

    # We clear devices to allow TensorFlow to control on which device it will
    # load operations
    clear_devices = True
    # We import the meta graph and retrieve a Saver
    saver = tf.train.import_meta_graph(input_checkpoint + '.meta',
                                       clear_devices=clear_devices)

    # We retrieve the protobuf graph definition
    graph = tf.get_default_graph()
    input_graph_def = graph.as_graph_def()

    # We start a session and restore the graph weights
    with tf.Session() as sess:
      saver.restore(sess, input_checkpoint)

      # We use a built-in TF helper to export variables to constants
      output_graph_def = graph_util.convert_variables_to_constants(
          sess, # The session is used to retrieve the weights
          input_graph_def, # The graph_def is used to retrieve the nodes
          output_node_names, # The output node names are used to select the
                             #usefull nodes
          variable_names_blacklist=['global_step'])

      # Finally we serialize and dump the output graph to the filesystem
      with tf.gfile.GFile(output_graph, "wb") as output_graph_file:
        output_graph_file.write(output_graph_def.SerializeToString())
      print("%d ops in the final graph." % len(output_graph_def.node))

  def __load_graph(self):
    """Load freezed graph."""
    # We load the protobuf file from the disk and parse it to retrieve the
    # unserialized graph_def
    with tf.gfile.GFile(self.frozen_graph_filename, "rb") as frozen_graph_file:
      graph_def = tf.GraphDef()
      graph_def.ParseFromString(frozen_graph_file.read())

    # Then, we import the graph_def into a new Graph and returns it
    with tf.Graph().as_default() as self.graph:
      # The name var will prefix every op/nodes in your graph
      # Since we load everything in a new graph, this is not needed
      tf.import_graph_def(graph_def, name="import")

  def __decode_from_file(self, filename):
    """Compute predictions on entries in filename and write them out."""

    if not self.decode_hp.batch_size:
      self.decode_hp.batch_size = 32
      tf.logging.info("decode_hp.batch_size not specified; default=%d" %
                      self.decode_hp.batch_size)

    p_hparams = self.hparams.problem_hparams
    inputs_vocab = p_hparams.vocabulary["inputs"]
    targets_vocab = p_hparams.vocabulary["targets"]
    problem_name = "grapheme_to_phoneme_problem"
    tf.logging.info("Performing decoding from a file.")
    inputs = _get_inputs(filename)
    num_decode_batches = (len(inputs) - 1) // self.decode_hp.batch_size + 1

    def input_fn():
      """Function for inputs generator."""
      input_gen = _decode_batch_input_fn(
          num_decode_batches, inputs, inputs_vocab,
          self.decode_hp.batch_size, self.decode_hp.max_input_size)
      gen_fn = decoding.make_input_fn_from_generator(input_gen)
      example = gen_fn()
      return decoding._decode_input_tensor_to_features_dict(example,
                                                            self.hparams)

    decodes = []
    result_iter = self.estimator.predict(input_fn)
    try:
      for result in result_iter:
        if self.decode_hp.return_beams:
          decoded_inputs = inputs_vocab.decode(
              decoding._save_until_eos(result["inputs"], False))
          beam_decodes = []
          output_beams = np.split(result["outputs"], self.decode_hp.beam_size,
                                  axis=0)
          for k, beam in enumerate(output_beams):
            decoded_outputs = targets_vocab.decode(
                decoding._save_until_eos(beam, False))
            beam_decodes.append(decoded_outputs)
          decodes.append(beam_decodes)
        else:
          decoded_inputs = inputs_vocab.decode(
              decoding._save_until_eos(result["inputs"], False))
          decoded_outputs = targets_vocab.decode(
              decoding._save_until_eos(result["outputs"], False))
          decodes.append(decoded_outputs)
    except:
      # raise StandardError("Invalid model in {}".format(self.params.model_dir))
      raise ValueError("Invalid model in {}".format(self.params.model_dir))

    return [inputs, decodes]

  def calc_errors(self, decode_file_path):
    """Calculate a number of word and phone prediction errors."""
    inputs, decodes = self.__decode_from_file(decode_file_path)

    word_correct, word_errors, phone_errors = 0, 0, 0
    total_ref_phones = 0
    word_set = set()
    for index, word in enumerate(inputs):
      if word in word_set:
        continue

      word_set.add(word)
      # Estimate #phones of the word
      ref_phone_count = np.mean([len(ref_str.split(" ")) 
                                 for ref_str in self.g2p_gt_map[word]])
      total_ref_phones += int(ref_phone_count)

      if self.decode_hp.return_beams:
        beam_correct_found = False
        for beam_decode in decodes[index]:
          if beam_decode in self.g2p_gt_map[word]:
            beam_correct_found = True
            break
        if beam_correct_found:
          word_correct += 1
        else:
          word_errors += 1
          # Estimate phone-level errors
          phone_error = phone_errors_for_single_word(decodes[index], 
                                                     self.g2p_gt_map[word])
          phone_errors += phone_error
          
      else:
        if decodes[index] in self.g2p_gt_map[word]:
          word_correct += 1
        else:
          word_errors += 1
          # Estimate phone-level errors
          phone_error = phone_errors_for_single_word([decodes[index]], 
                                                     self.g2p_gt_map[word])
          phone_errors += phone_error

    return word_correct, word_errors, phone_errors, total_ref_phones


def phone_errors_for_single_word(predicted_strs, ref_strs):
  """
  Given decoded results (depending on beam size) and a list of ref 
  pronunciations, estimate the phone-level edit distance. Return the min
  distance.
  """
  phone_error_list = []
  for ref_str in ref_strs:
    for predicted_str in predicted_strs:
      d = phone_edit_distance(predicted_str, ref_str)
      phone_error_list.append(d)
  return min(phone_error_list)


def phone_edit_distance(predicted_str, ref_str):
  """
  Estimate the edit distance between predicted and ref phone sequences.
  """
  predicted_list = predicted_str.split(" ")
  ref_list = ref_str.split(" ")
  m, n = len(predicted_list), len(ref_list)
  dp = [[0] * (m+1) for _ in range(n+1)]
  dp[0][0] = 0
        
  for i in range(1, m+1):
    dp[0][i] = i
        
  for i in range(1, n+1):
    dp[i][0] = i
        
  for i in range(1, m+1):
    for j in range(1, n+1):
      if predicted_list[i-1] == ref_list[j-1]:
        dp[j][i] = dp[j-1][i-1]
      else:
        dp[j][i] = min(dp[j-1][i] + 1, dp[j][i-1] + 1, dp[j-1][i-1] + 1)
                
  return dp[n][m]


def get_word():
  """Get next word in the interactive mode."""
  word = ""
  try:
    word = input("> ")
    #if not issubclass(type(word), text_type):
    #  word = text_type(word, encoding="utf-8", errors="replace")
  except EOFError:
    pass
  if not word:
    pass
  return word


def create_g2p_gt_map(words, pronunciations):
  """Create grapheme-to-phoneme ground true mapping."""
  g2p_gt_map = {}
  for word, pronunciation in zip(words, pronunciations):
    if word in g2p_gt_map:
      g2p_gt_map[word].append(pronunciation)
    else:
      g2p_gt_map[word] = [pronunciation]
  return g2p_gt_map


def _get_inputs(filename, delimiters="\t "):
  """Returning inputs.

  Args:
    filename: path to file with inputs, 1 per line.
    delimiters: str, delimits records in the file.

  Returns:
    a list of inputs

  """
  tf.logging.info("Getting inputs")
  delimiters_regex = re.compile("[" + delimiters + "]+")

  inputs = []
  with tf.gfile.Open(filename) as input_file:
    lines = input_file.readlines()
    for line in lines:
      if set("[" + delimiters + "]+$").intersection(line):
        items = re.split(delimiters_regex, line.strip(), maxsplit=1)
        inputs.append(items[0])
      else:
        inputs.append(line.strip())
  return inputs


def _decode_batch_input_fn(num_decode_batches, inputs,
                           vocabulary, batch_size, max_input_size):
  """Decode batch"""
  for batch_idx in range(num_decode_batches):
    tf.logging.info("Decoding batch %d out of %d" % (batch_idx, num_decode_batches))
    batch_length = 0
    batch_inputs = []
    for _inputs in inputs[batch_idx * batch_size:(batch_idx + 1) * batch_size]:
      input_ids = vocabulary.encode(_inputs)
      if max_input_size > 0:
        # Subtract 1 for the EOS_ID.
        input_ids = input_ids[:max_input_size - 1]
      input_ids.append(text_encoder.EOS_ID)
      batch_inputs.append(input_ids)
      if len(input_ids) > batch_length:
        batch_length = len(input_ids)
    final_batch_inputs = []
    for input_ids in batch_inputs:
      assert len(input_ids) <= batch_length
      encoded_input = input_ids + [0] * (batch_length - len(input_ids))
      final_batch_inputs.append(encoded_input)

    yield {
        "inputs": np.array(final_batch_inputs).astype(np.int32),
        "problem_choice": np.array(0).astype(np.int32),
    }


def execute_schedule(exp, params):
  if not hasattr(exp, params.schedule):
    raise ValueError(
            "Experiment has no method %s, from --schedule" % params.schedule)
  with profile_context(params):
    getattr(exp, params.schedule)()


@contextlib.contextmanager
def profile_context(params):
  if params.profile:
    with tf.contrib.tfprof.ProfileContext("t2tprof",
            trace_steps=range(100),
            dump_steps=range(100)) as pctx:
      opts = tf.profiler.ProfileOptionBuilder.time_and_memory()
      pctx.add_auto_profiling("op", opts, range(100))
      yield
  else:
    yield
