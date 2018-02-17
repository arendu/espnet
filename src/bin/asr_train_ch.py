#!/usr/bin/env python

# Copyright 2017 Johns Hopkins University (Shinji Watanabe)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)


import collections
import copy
import json
import logging
import math
import os
import six

# chainer related
import chainer
from chainer import cuda
from chainer import function
from chainer import reporter as reporter_module
from chainer import training
from chainer.training import extensions

# espnet related
from asr_train_utils import CompareValueTrigger
from asr_train_utils import converter_kaldi
from asr_train_utils import delete_feat
from asr_train_utils import make_batchset
from asr_train_utils import restore_snapshot
from e2e_asr_attctc import E2E
from e2e_asr_attctc import Loss

# for kaldi io
import lazy_io

# numpy related
import matplotlib
import numpy as np
matplotlib.use('Agg')


class SeqEvaluaterKaldi(extensions.Evaluator):
    '''Custom evaluater with Kaldi reader'''
    def __init__(self, iterator, target, reader, device):
        super(SeqEvaluaterKaldi, self).__init__(
            iterator, target, device=device)
        self.reader = reader

    # The core part of the update routine can be customized by overriding.
    def evaluate(self):
        iterator = self._iterators['main']
        eval_func = self.eval_func or self._targets['main']

        if self.eval_hook:
            self.eval_hook(self)

        if hasattr(iterator, 'reset'):
            iterator.reset()
            it = iterator
        else:
            it = copy.copy(iterator)

        summary = reporter_module.DictSummary()

        for batch in it:
            observation = {}
            with reporter_module.report_scope(observation):
                # read scp files
                # x: original json with loaded features
                #    will be converted to chainer variable later
                # batch only has one minibatch utterance, which is specified by batch[0]
                x = converter_kaldi(batch[0], self.reader)
                with function.no_backprop_mode():
                    eval_func(x)
                    delete_feat(x)

            summary.add(observation)

        return summary.compute_mean()


class SeqUpdaterKaldi(training.StandardUpdater):
    '''Custom updater with Kaldi reader'''
    def __init__(self, train_iter, optimizer, reader, device):
        super(SeqUpdaterKaldi, self).__init__(
            train_iter, optimizer, device=device)
        self.reader = reader

    # The core part of the update routine can be customized by overriding.
    def update_core(self):
        # When we pass one iterator and optimizer to StandardUpdater.__init__,
        # they are automatically named 'main'.
        train_iter = self.get_iterator('main')
        optimizer = self.get_optimizer('main')

        # Get the next batch ( a list of json files)
        batch = train_iter.__next__()

        # read scp files
        # x: original json with loaded features
        #    will be converted to chainer variable later
        # batch only has one minibatch utterance, which is specified by batch[0]
        x = converter_kaldi(batch[0], self.reader)

        # Compute the loss at this time step and accumulate it
        loss = optimizer.target(x)
        optimizer.target.cleargrads()  # Clear the parameter gradients
        loss.backward()  # Backprop
        loss.unchain_backward()  # Truncate the graph
        # compute the gradient norm to check if it is normal or not
        grad_norm = np.sqrt(self._sum_sqnorm(
            [p.grad for p in optimizer.target.params(False)]))
        logging.info('grad norm={}'.format(grad_norm))
        if math.isnan(grad_norm):
            logging.warning('grad norm is nan. Do not update model.')
        else:
            optimizer.update()
        delete_feat(x)

    # copied from https://github.com/chainer/chainer/blob/master/chainer/optimizer.py
    def _sum_sqnorm(self, arr):
        sq_sum = collections.defaultdict(float)
        for x in arr:
            with cuda.get_device_from_array(x) as dev:
                x = x.ravel()
                s = x.dot(x)
                sq_sum[int(dev)] += s
        return sum([float(i) for i in six.itervalues(sq_sum)])


def adadelta_eps_decay(eps_decay):
    '''Extension to perform adadelta eps decay'''
    @training.make_extension(trigger=(1, 'epoch'))
    def adadelta_eps_decay(trainer):
        _adadelta_eps_decay(trainer, eps_decay)

    return adadelta_eps_decay


def _adadelta_eps_decay(trainer, eps_decay):
    optimizer = trainer.updater.get_optimizer('main')
    current_eps = optimizer.eps
    setattr(optimizer, 'eps', current_eps * eps_decay)
    logging.info('adadelta eps decayed to ' + str(optimizer.eps))


def train(args):
    # display chainer version
    logging.info('chainer version = ' + chainer.__version__)

    # seed setting (chainer seed may not need it)
    nseed = args.seed
    os.environ['CHAINER_SEED'] = str(nseed)
    logging.info('chainer seed = ' + os.environ['CHAINER_SEED'])

    # debug mode setting
    # 0 would be fastest, but 1 seems to be reasonable
    # by considering reproducability
    # revmoe type check
    if args.debugmode < 2:
        chainer.config.type_check = False
        logging.info('chainer type check is disabled')
    # use determinisitic computation or not
    if args.debugmode < 1:
        chainer.config.cudnn_deterministic = False
        logging.info('chainer cudnn deterministic is disabled')
    else:
        chainer.config.cudnn_deterministic = True

    # check cuda and cudnn availability
    if not chainer.cuda.available:
        logging.warning('cuda is not available')
    if not chainer.cuda.cudnn_enabled:
        logging.warning('cudnn is not available')

    # get input and output dimension info
    with open(args.valid_label, 'rb') as f:
        valid_json = json.load(f)['utts']
    utts = list(valid_json.keys())
    idim = int(valid_json[utts[0]]['idim'])
    odim = int(valid_json[utts[0]]['odim'])
    logging.info('#input dims : ' + str(idim))
    logging.info('#output dims: ' + str(odim))

    # specify model architecture
    e2e = E2E(idim, odim, args)
    model = Loss(e2e, args.mtlalpha)

    # Set gpu
    gpu_id = int(args.gpu)
    logging.info('gpu id: ' + str(gpu_id))
    if gpu_id >= 0:
        # Make a specified GPU current
        chainer.cuda.get_device_from_id(gpu_id).use()
        model.to_gpu()  # Copy the model to the GPU

    # Setup an optimizer
    if args.opt == 'adadelta':
        optimizer = chainer.optimizers.AdaDelta(eps=args.eps)
    elif args.opt == 'adam':
        optimizer = chainer.optimizers.Adam()
    optimizer.setup(model)
    optimizer.add_hook(chainer.optimizer.GradientClipping(args.grad_clip))

    # read json data
    with open(args.train_label, 'rb') as f:
        train_json = json.load(f)['utts']
    with open(args.valid_label, 'rb') as f:
        valid_json = json.load(f)['utts']

    # make minibatch list (variable length)
    train = make_batchset(train_json, args.batch_size,
                          args.maxlen_in, args.maxlen_out, args.minibatches)
    valid = make_batchset(valid_json, args.batch_size,
                          args.maxlen_in, args.maxlen_out, args.minibatches)
    # hack to make batchsze argument as 1
    # actual bathsize is included in a list
    train_iter = chainer.iterators.SerialIterator(train, 1)
    valid_iter = chainer.iterators.SerialIterator(
        valid, 1, repeat=False, shuffle=False)

    # prepare Kaldi reader
    train_reader = lazy_io.read_dict_scp(args.train_feat)
    valid_reader = lazy_io.read_dict_scp(args.valid_feat)

    # Set up a trainer
    updater = SeqUpdaterKaldi(train_iter, optimizer, train_reader, gpu_id)
    trainer = training.Trainer(
        updater, (args.epochs, 'epoch'), out=args.outdir)

    # Resume from a snapshot
    if args.resume:
        chainer.serializers.load_npz(args.resume, trainer)

    # Evaluate the model with the test dataset for each epoch
    trainer.extend(SeqEvaluaterKaldi(
        valid_iter, model, valid_reader, device=gpu_id))

    # Take a snapshot for each specified epoch
    trainer.extend(extensions.snapshot(), trigger=(1, 'epoch'))

    # Make a plot for training and validation values
    trainer.extend(extensions.PlotReport(['main/loss', 'validation/main/loss',
                                          'main/loss_ctc', 'validation/main/loss_ctc',
                                          'main/loss_att', 'validation/main/loss_att'],
                                         'epoch', file_name='loss.png'))
    trainer.extend(extensions.PlotReport(['main/acc', 'validation/main/acc'],
                                         'epoch', file_name='acc.png'))

    # Save best models
    trainer.extend(extensions.snapshot_object(model, 'model.loss.best'),
                   trigger=training.triggers.MinValueTrigger('validation/main/loss'))
    trainer.extend(extensions.snapshot_object(model, 'model.acc.best'),
                   trigger=training.triggers.MaxValueTrigger('validation/main/acc'))

    # epsilon decay in the optimizer
    if args.opt == 'adadelta':
        if args.criterion == 'acc':
            trainer.extend(restore_snapshot(model, args.outdir + '/model.acc.best'),
                           trigger=CompareValueTrigger(
                               'validation/main/acc',
                               lambda best_value, current_value: best_value > current_value))
            trainer.extend(adadelta_eps_decay(args.eps_decay),
                           trigger=CompareValueTrigger(
                               'validation/main/acc',
                               lambda best_value, current_value: best_value > current_value))
        elif args.criterion == 'loss':
            trainer.extend(restore_snapshot(model, args.outdir + '/model.loss.best'),
                           trigger=CompareValueTrigger(
                               'validation/main/loss',
                               lambda best_value, current_value: best_value < current_value))
            trainer.extend(adadelta_eps_decay(args.eps_decay),
                           trigger=CompareValueTrigger(
                               'validation/main/loss',
                               lambda best_value, current_value: best_value < current_value))

    # Write a log of evaluation statistics for each epoch
    trainer.extend(extensions.LogReport(trigger=(100, 'iteration')))
    report_keys = ['epoch', 'iteration', 'main/loss', 'main/loss_ctc', 'main/loss_att',
                   'validation/main/loss', 'validation/main/loss_ctc', 'validation/main/loss_att',
                   'main/acc', 'validation/main/acc', 'elapsed_time']
    if args.opt == 'adadelta':
        trainer.extend(extensions.observe_value(
            'eps', lambda trainer: trainer.updater.get_optimizer('main').eps),
            trigger=(100, 'iteration'))
        report_keys.append('eps')
    trainer.extend(extensions.PrintReport(
        report_keys), trigger=(100, 'iteration'))

    trainer.extend(extensions.ProgressBar())

    # Run the training
    trainer.run()