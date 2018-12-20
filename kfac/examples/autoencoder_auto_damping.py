# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
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
"""Implementation of Deep AutoEncoder from Martens & Grosse (2015).

This script demonstrates training using KFAC optimizer and updating the
damping parameter according to the Levenberg-Marquardt rule.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
# Dependency imports
from absl import flags
import kfac
import sonnet as snt
import tensorflow as tf

from kfac.examples import mnist
from kfac.python.ops.kfac_utils import data_reader
from kfac.python.ops.kfac_utils import data_reader_alt


# Model parameters
_ENCODER_SIZES = [1000, 500, 250, 30]
_DECODER_SIZES = [250, 500, 1000]
_WEIGHT_DECAY = 1e-5
_NONLINEARITY = tf.tanh  # Note: sigmoid cannot be used with the default init.
_WEIGHTS_INITIALIZER = None  # Default init


flags.DEFINE_integer('inverse_update_period', 10,
                     '# of steps between computing inverse of fisher factor '
                     'matrices.')
flags.DEFINE_integer('cov_update_period', 1,
                     '# of steps between computing covaraiance matrices.')
flags.DEFINE_integer('damping_adaptation_interval', 5,
                     '# of steps between updating the damping parameter.')

flags.DEFINE_float('learning_rate', 5e-3,
                   'Learning rate to use when adaptation="off".')
flags.DEFINE_float('momentum', 0.9,
                   'Momentum decay value to use when '
                   'lrmu_adaptation="off" or "only_lr".')

flags.DEFINE_float('weight_decay', 1e-5,
                   'L2 regularization applied to weight matrices.')

flags.DEFINE_string('data_dir', '/tmp/mnist', 'local mnist dir')

flags.DEFINE_boolean('use_batch_size_schedule', True,
                     'If True then we use the growing mini-batch schedule from '
                     'the original K-FAC paper.')
flags.DEFINE_integer('batch_size', 1024,
                     'The size of the mini-batches to use if not using the '
                     'schedule.')

flags.DEFINE_string('lrmu_adaptation', 'on',
                    'If set to "on" then we use the quadratic model '
                    'based learning-rate and momentum adaptation method from '
                    'the original paper. Note that this only works well in '
                    'practice when use_batch_size_schedule=True. Can also '
                    'be set to "off" and "only_lr", which turns '
                    'it off, or uses a version where the momentum parameter '
                    'is fixed (resp.).')


flags.DEFINE_boolean('use_alt_data_reader', True,
                     'If True we use the alternative data reader for MNIST '
                     'that is faster for small datasets.')


FLAGS = flags.FLAGS


def make_train_op(batch_size,
                  batch_loss,
                  layer_collection,
                  loss_fn,
                  cached_reader):
  """Constructs optimizer and train op.

  Args:
    batch_size: Tensor of shape (), Size of the training batch.
    batch_loss: Tensor of shape (), Loss with respect to minibatch to be
      minimzed.
    layer_collection: LayerCollection or None. Registry for model parameters.
      Required when using a K-FAC optimizer.
    loss_fn: Function which takes as input training data and returns loss.
    cached_reader: `data_reader.CachedReader` instance.

  Returns:
    train_op: Op that can be used to update model parameters.
    optimizer: Optimizer used to produce train_op.

  Raises:
    ValueError: If layer_collection is None when K-FAC is selected as an
      optimization method.
  """
  global_step = tf.train.get_or_create_global_step()

  if layer_collection is None:
    raise ValueError('layer_collection must be defined to use K-FAC.')

  if FLAGS.lrmu_adaptation == 'on':
    learning_rate = 1.0
    momentum = None
    momentum_type = 'qmodel'
  elif FLAGS.lrmu_adaptation == 'only_lr':
    learning_rate = 1.0
    momentum = FLAGS.momentum
    momentum_type = 'qmodel_fixedmu'
  elif FLAGS.lrmu_adaptation == 'off':
    learning_rate = FLAGS.learning_rate
    momentum = FLAGS.momentum
    momentum_type = 'regular'
    # momentum_type = 'adam'

  optimizer = kfac.PeriodicInvCovUpdateKfacOpt(
      invert_every=FLAGS.inverse_update_period,
      cov_update_every=FLAGS.cov_update_period,
      learning_rate=learning_rate,
      damping=150.,  # When using damping adaptation it is advisable to start
                     # with a high value
      cov_ema_decay=0.95,
      momentum=momentum,
      momentum_type=momentum_type,
      layer_collection=layer_collection,
      batch_size=batch_size,
      adapt_damping=True,
      is_chief=True,
      prev_train_batch=cached_reader.cached_batch,
      loss_fn=loss_fn,
      damping_adaptation_decay=0.95,
      damping_adaptation_interval=FLAGS.damping_adaptation_interval,
      min_damping=FLAGS.weight_decay
      )
  return optimizer.minimize(batch_loss, global_step=global_step), optimizer


class AutoEncoder(snt.AbstractModule):
  """Simple autoencoder module."""

  def __init__(self,
               input_size,
               regularizers=None,
               initializers=None,
               custom_getter=None,
               name='AutoEncoder'):
    super(AutoEncoder, self).__init__(name=name)

    if initializers is None:
      initializers = {'w': tf.glorot_uniform_initializer(),
                      'b': tf.zeros_initializer()}
    if regularizers is None:
      regularizers = {'w': lambda w: _WEIGHT_DECAY*tf.nn.l2_loss(w),
                      'b': lambda w: _WEIGHT_DECAY*tf.nn.l2_loss(w),}

    with self._enter_variable_scope():
      self._encoder = snt.nets.MLP(
          output_sizes=_ENCODER_SIZES,
          regularizers=regularizers,
          initializers=initializers,
          custom_getter=custom_getter,
          activation=_NONLINEARITY,
          activate_final=False)
      self._decoder = snt.nets.MLP(
          output_sizes=_DECODER_SIZES + [input_size],
          regularizers=regularizers,
          initializers=initializers,
          custom_getter=custom_getter,
          activation=_NONLINEARITY,
          activate_final=False)

  def _build(self, inputs):
    code = self._encoder(inputs)
    output = self._decoder(code)

    return output


def compute_accuracy(logits=None, labels=None):
  """Compute mean squared error."""
  return tf.reduce_sum(tf.reduce_mean(tf.square(labels - tf.nn.sigmoid(logits)),
                                      axis=0))


def compute_loss(logits=None,
                 labels=None,
                 layer_collection=None,
                 return_acc=False):
  """Compute loss value."""
  graph_regularizers = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
  total_regularization_loss = tf.reduce_sum(graph_regularizers)
  loss_matrix = tf.nn.sigmoid_cross_entropy_with_logits(logits=logits,
                                                        labels=labels)
  loss = tf.reduce_sum(tf.reduce_mean(loss_matrix, axis=0))
  regularized_loss = loss + total_regularization_loss

  if layer_collection is not None:
    layer_collection.register_multi_bernoulli_predictive_distribution(logits)
    layer_collection.auto_register_layers()

  if return_acc:
    accuracy = compute_accuracy(logits=logits, labels=labels)
    return regularized_loss, accuracy

  return regularized_loss


def load_mnist():
  """Creates MNIST dataset and wraps it inside cached data reader.

  Returns:
    cached_reader: `data_reader.CachedReader` instance which wraps MNIST
      dataset.
    training_batch: Tensor of shape `[batch_size, 784]`, MNIST training images.
  """
  # Wrap the data set into cached_reader which provides variable sized training
  # and caches the read train batch.

  if not FLAGS.use_alt_data_reader:
    # Version 1 using data_reader.py (slow!)
    dataset, num_examples = mnist.load_mnist_as_dataset(
        FLAGS.data_dir,
        flatten_images=True)
    if FLAGS.use_batch_size_schedule:
      max_batch_size = num_examples
    else:
      max_batch_size = FLAGS.batch_size

    # Shuffle before repeat is correct unless you want repeat cases in the
    # same batch.
    dataset = (dataset.shuffle(num_examples).repeat()
               .batch(max_batch_size).prefetch(5))
    dataset = tf.compat.v1.data.make_one_shot_iterator(dataset).get_next()

    # This version of CachedDataReader requires the dataset to be shuffled
    return data_reader.CachedDataReader(dataset, max_batch_size)

  else:
    # Version 2 using data_reader_alt.py (faster)
    images, labels, num_examples = mnist.load_mnist_as_tensors(
        FLAGS.data_dir,
        flatten_images=True)
    dataset = (images, labels)

    # This version of CachedDataReader requires the dataset to NOT be shuffled
    return data_reader_alt.CachedDataReader(dataset, num_examples)


def main(_):
  # Load dataset.
  batch_size_schedule = [
      int(min(1000 * math.exp(k / 124.25), 55000)) for k in range(500)
  ]
  batch_size = tf.placeholder(shape=(), dtype=tf.int32, name='batch_size')
  cached_reader = load_mnist()

  # Create autoencoder model using Soham's code instead
  training_model = AutoEncoder(784)
  layer_collection = kfac.LayerCollection()

  def loss_fn(minibatch, layer_collection=None, return_acc=False):
    input_ = minibatch[0]
    logits = training_model(input_)

    return compute_loss(
        logits=logits,
        labels=input_,
        layer_collection=layer_collection,
        return_acc=return_acc)

  minibatch = cached_reader(batch_size)
  batch_loss, batch_error = loss_fn(minibatch,
                                    layer_collection=layer_collection,
                                    return_acc=True)
  # Make training op
  train_op, opt = make_train_op(
      batch_size,
      batch_loss,
      layer_collection,
      loss_fn=loss_fn,
      cached_reader=cached_reader)

  learning_rate = opt.learning_rate
  momentum = opt.momentum

  # Fit model.
  global_step = tf.train.get_or_create_global_step()
  with tf.train.MonitoredTrainingSession(save_checkpoint_secs=30) as sess:
    while not sess.should_stop():
      i = sess.run(global_step)

      if FLAGS.use_batch_size_schedule:
        batch_size_ = batch_size_schedule[min(i, len(batch_size_schedule) - 1)]
      else:
        batch_size_ = FLAGS.batch_size

      _, batch_loss_, batch_error_, learning_rate_, momentum_ = sess.run(
          [train_op, batch_loss, batch_error, learning_rate, momentum],
          feed_dict={batch_size: batch_size_})

      # We get these things in a separate sess.run() call because they are
      # stored as variables in the optimizer. (So there is no computational cost
      # to getting them, and if we don't get them after the previous call is
      # over they might not be updated.)
      damping_, rho_, qmodel_change_ = sess.run(
          [opt.damping, opt.rho, opt.qmodel_change])

      # Print training stats.
      tf.logging.info(
          'iteration: %d', i)
      tf.logging.info(
          'mini-batch size: %d | mini-batch loss = %f | mini-batch error = %f ',
          batch_size_, batch_loss_, batch_error_)
      tf.logging.info(
          'learning_rate = %f | momentum = %f',
          learning_rate_, momentum_)
      tf.logging.info(
          'damping = %f | rho = %f | qmodel_change = %f',
          damping_, rho_, qmodel_change_)
      tf.logging.info('----')


if __name__ == '__main__':
  tf.app.run(main)


