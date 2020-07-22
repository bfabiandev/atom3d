from __future__ import division, print_function, absolute_import

import argparse
import functools
import json
import logging
import math
import os
import random

import numpy as np
import pandas as pd
import sklearn.metrics as sm
import tensorflow as tf
import tqdm

try:
    tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)
except:
    pass

import atom3d.util.shard as sh
import examples.cnn3d.model as model
import examples.cnn3d.feature_mut as feature_mut
import examples.cnn3d.subgrid_gen as subgrid_gen

import dotenv as de
de.load_dotenv(de.find_dotenv(usecwd=True))


def major_vote(results):
    data = []
    for ensemble, df in results.groupby('ensembles'):
        true = int(df['true'].unique()[0])
        zeros = np.sum(df['pred'] == '0')
        ones = np.sum(df['pred'] == '1')
        majority_pred = int(df['pred'].mode().values[0])
        data.append([ensemble, true, majority_pred, zeros, ones])
    vote_df = pd.DataFrame(data, columns=['ensembles', 'true', 'pred', '0', '1'])
    return vote_df


def compute_perf(df):
    results = major_vote(df)
    res = {}
    all_trues = results['true'].astype(np.int8)
    all_preds = results['pred'].astype(np.int8)
    res['all_ap'] = sm.average_precision_score(all_trues, all_preds)
    res['all_auroc'] = sm.roc_auc_score(all_trues, all_preds)
    res['all_acc'] = sm.accuracy_score(all_trues, all_preds.round())
    res['all_bal_acc'] = \
        sm.balanced_accuracy_score(all_trues, all_preds.round())
    res['all_loss'] = sm.log_loss(all_trues, all_preds)
    return res


def __stats(mode, df):
    # Compute stats
    res = compute_perf(df)
    logging.info(
        '\n{:}\n'
        'Perf Metrics:\n'
        '    AP: {:.3f}\n'
        '    AUROC: {:.3f}\n'
        '    Accuracy: {:.3f}\n'
        '    Balanced Accuracy: {:.3f}\n'
        '    Log loss: {:.3f}'.format(
        mode,
        float(res["all_ap"]),
        float(res["all_auroc"]),
        float(res["all_acc"]),
        float(res["all_bal_acc"]),
        float(res["all_loss"])))


def __channel_size(args):
    size = subgrid_gen.num_channels(args.grid_config)
    if args.add_flag:
        size += 1
    return size


def compute_accuracy(true_y, predicted_y):
    true_y = tf.cast(true_y, tf.float32)
    correct_prediction =  \
        tf.logical_or(
            tf.logical_and(
                tf.less_equal(predicted_y, 0.5),
                tf.less_equal(true_y, 0.5)),
            tf.logical_and(
                tf.greater(predicted_y, 0.5),
                tf.greater(true_y, 0.5)))
    return tf.cast(correct_prediction, tf.float32, name='accuracy')


def balanced_loss(true_y, logits, pos_weight):
    with tf.variable_scope('loss'):
        # losses : [B]
        losses = tf.nn.weighted_cross_entropy_with_logits(
            targets=tf.cast(true_y, tf.float32), logits=logits,
            pos_weight=pos_weight)

        # We reweight the losses so that the mean is comparable across
        # different neg to pos ratios.
        batch_size = tf.cast(tf.shape(true_y)[0], tf.float32)
        num_pos = tf.count_nonzero(true_y, dtype=tf.float32)
        num_neg = batch_size - num_pos
        effective_weight = num_pos * pos_weight + num_neg
        losses = losses / tf.cast(effective_weight, tf.float32) * \
            tf.cast(batch_size, tf.float32)
        losses = tf.identity(losses, name='cross_entropy')
    return losses


# Construct model and loss
def conv_model(feature, target, is_training, conv_drop_rate, fc_drop_rate,
               top_nn_drop_rate, args):
    num_conv = args.num_conv
    conv_filters = [32 * (2**n) for n in range(num_conv)]
    conv_kernel_size = 3
    max_pool_positions = [0, 1]*int((num_conv+1)/2)
    max_pool_sizes = [2]*num_conv
    max_pool_strides = [2]*num_conv
    fc_units = [512]
    top_fc_units = [512]*args.num_final_fc_layers

    logits = model.siamese_model(
        feature,
        is_training,
        conv_drop_rate,
        fc_drop_rate,
        top_nn_drop_rate,
        conv_filters, conv_kernel_size,
        max_pool_positions,
        max_pool_sizes, max_pool_strides,
        fc_units,
        top_fc_units,
        batch_norm=args.use_batch_norm,
        dropout=not args.no_dropout,
        top_nn_activation=args.top_nn_activation)

    # Prediction
    predict = tf.round(tf.nn.sigmoid(logits), name='predict')

    # Loss
    loss = balanced_loss(target, logits, args.grid_config.neg_to_pos_ratio)

    # Accuracy
    accuracy = compute_accuracy(target, predict)
    return logits, predict, loss, accuracy


def batch_dataset_generator(gen, args, is_testing=False):
    grid_size = subgrid_gen.grid_size(args.grid_config)
    channel_size = __channel_size(args)
    dataset = tf.data.Dataset.from_generator(
        gen,
        output_types=(tf.string, tf.float32, tf.float32),
        output_shapes=((), (2, grid_size, grid_size, grid_size, channel_size), (1,))
        )

    # Shuffle dataset
    if not is_testing:
        if args.shuffle:
            dataset = dataset.repeat(count=None)
        else:
            dataset = dataset.apply(
                tf.contrib.data.shuffle_and_repeat(buffer_size=1000))

    dataset = dataset.batch(args.batch_size)
    dataset = dataset.prefetch(8)

    iterator = dataset.make_one_shot_iterator()
    next_element = iterator.get_next()
    return dataset, next_element


def train_model(sess, args):
    # tf Graph input
    # Subgrid maps for each residue in a protein
    logging.debug('Create input placeholder...')
    grid_size = subgrid_gen.grid_size(args.grid_config)
    channel_size = __channel_size(args)
    feature_placeholder = tf.placeholder(
        tf.float32,
        [None, 2, grid_size, grid_size, grid_size, channel_size],
        name='main_input')
    label_placeholder = tf.placeholder(tf.int8, [None, 1], 'label')

    # Placeholder for model parameters
    training_placeholder = tf.placeholder(tf.bool, shape=[], name='is_training')
    conv_drop_rate_placeholder = tf.placeholder(tf.float32, name='conv_drop_rate')
    fc_drop_rate_placeholder = tf.placeholder(tf.float32, name='fc_drop_rate')
    top_nn_drop_rate_placeholder = tf.placeholder(tf.float32, name='top_nn_drop_rate')

    # Define loss and optimizer
    logging.debug('Define loss and optimizer...')
    logits_op, predict_op, loss_op, accuracy_op = conv_model(
        feature_placeholder, label_placeholder, training_placeholder,
        conv_drop_rate_placeholder, fc_drop_rate_placeholder,
        top_nn_drop_rate_placeholder, args)
    logging.debug('Generate training ops...')
    train_op = model.training(loss_op, args.learning_rate)

    # Initialize the variables (i.e. assign their default value)
    logging.debug('Initializing global variables...')
    init = tf.global_variables_initializer()

    # Create saver and summaries.
    logging.debug('Initializing saver...')
    saver = tf.train.Saver(max_to_keep=100000)
    logging.debug('Finished initializing saver...')

    def __loop(generator, mode, num_iters):
        tf_dataset, next_element = batch_dataset_generator(
            generator, args, is_testing=(mode=='test'))

        ensembles, losses, logits, preds, labels = [], [], [], [], []
        epoch_loss = 0
        epoch_acc = 0
        progress_format = mode + ' loss: {:6.6f}' + '; acc: {:6.4f}'

        # Loop over all batches (one batch is all feature for 1 protein)
        num_batches = int(math.ceil(float(num_iters)/args.batch_size))
        #print('\nRunning {:} -> {:} iters in {:} batches (batch size: {:})'.format(
        #    mode, num_iters, num_batches, args.batch_size))
        with tqdm.tqdm(total=num_batches, desc=progress_format.format(0, 0)) as t:
            for i in range(num_batches):
                try:
                    ensemble_, feature_, label_ = sess.run(next_element)
                    _, logit, pred, loss, accuracy = sess.run(
                        [train_op, logits_op, predict_op, loss_op, accuracy_op],
                        feed_dict={feature_placeholder: feature_,
                                   label_placeholder: label_,
                                   training_placeholder: (mode == 'train'),
                                   conv_drop_rate_placeholder:
                                       args.conv_drop_rate if mode == 'train' else 0.0,
                                   fc_drop_rate_placeholder:
                                       args.fc_drop_rate if mode == 'train' else 0.0,
                                   top_nn_drop_rate_placeholder:
                                       args.top_nn_drop_rate if mode == 'train' else 0.0})
                    #print('logit: {:}, predict: {:}, loss: {:.3f}, actual: {:}'.format(logit, pred, loss, label_))
                    epoch_loss += (np.mean(loss) - epoch_loss) / (i + 1)
                    epoch_acc += (np.mean(accuracy) - epoch_acc) / (i + 1)
                    ensembles.extend(ensemble_.astype(str))
                    losses.append(loss)
                    logits.extend(logit.astype(np.float))
                    preds.extend(pred.astype(np.int8))
                    labels.extend(label_.astype(np.int8))

                    t.set_description(progress_format.format(epoch_loss, epoch_acc))
                    t.update(1)
                except (tf.errors.OutOfRangeError, StopIteration):
                    logging.info("\nEnd of {:} dataset at iteration {:}".format(mode, i))
                    break

        def __concatenate(array):
            try:
                array = np.concatenate(array)
                return array
            except:
                return array

        ensembles = __concatenate(ensembles)
        logits = __concatenate(logits)
        preds = __concatenate(preds)
        labels = __concatenate(labels)
        losses = __concatenate(losses)
        return ensembles, logits, preds, labels, losses, epoch_loss

    # Run the initializer
    logging.debug('Running initializer...')
    sess.run(init)
    logging.debug('Finished running initializer...')

    ##### Training + validation
    if not args.test_only:
        prev_val_loss, best_val_loss = float("inf"), float("inf")

        multiplier = args.grid_config.max_pos_per_shard * int(
            1 + args.grid_config.neg_to_pos_ratio)

        train_num_ensembles = args.train_sharded.get_num_shards()*multiplier
        train_num_ensembles *= args.repeat_gen

        val_num_ensembles = args.val_sharded.get_num_shards()*multiplier
        val_num_ensembles *= args.repeat_gen

        logging.info("Start training with {:} ensembles for train and {:} ensembles for val per epoch".format(
            train_num_ensembles, val_num_ensembles))


        def _save():
            ckpt = saver.save(sess, os.path.join(args.output_dir, 'model-ckpt'),
                              global_step=epoch)
            return ckpt

        run_info_filename = os.path.join(args.output_dir, 'run_info.json')
        run_info = {}
        def __update_and_write_run_info(key, val):
            run_info[key] = val
            with open(run_info_filename, 'w') as f:
                json.dump(run_info, f, indent=4)

        per_epoch_val_losses = []
        for epoch in range(1, args.num_epochs+1):
            random_seed = args.random_seed #random.randint(1, 10e6)
            logging.info('Epoch {:} - random_seed: {:}'.format(epoch, args.random_seed))

            logging.debug('Creating train generator...')
            train_generator_callable = functools.partial(
                feature_mut.dataset_generator,
                args.train_sharded,
                args.grid_config,
                shuffle=args.shuffle,
                repeat=args.repeat_gen,
                add_flag=args.add_flag,
                center_at_mut=args.center_at_mut,
                testing=False,
                random_seed=random_seed)

            logging.debug('Creating val generator...')
            val_generator_callable = functools.partial(
                feature_mut.dataset_generator,
                args.val_sharded,
                args.grid_config,
                shuffle=args.shuffle,
                repeat=args.repeat_gen,
                add_flag=args.add_flag,
                center_at_mut=args.center_at_mut,
                testing=False,
                random_seed=random_seed)

            # Training
            train_ensembles, train_logits, train_preds, train_labels, _, curr_train_loss = __loop(
                train_generator_callable, 'train', num_iters=train_num_ensembles)
            # Validation
            val_ensembles, val_logits, val_preds, val_labels, _, curr_val_loss = __loop(
                val_generator_callable, 'val', num_iters=val_num_ensembles)

            per_epoch_val_losses.append(curr_val_loss)
            __update_and_write_run_info('val_losses', per_epoch_val_losses)

            if args.use_best or args.early_stopping:
                if curr_val_loss < best_val_loss:
                    # Found new best epoch.
                    best_val_loss = curr_val_loss
                    ckpt = _save()
                    __update_and_write_run_info('val_best_loss', best_val_loss)
                    __update_and_write_run_info('best_ckpt', ckpt)
                    logging.info("New best {:}".format(ckpt))

            if (epoch == args.num_epochs - 1 and not args.use_best):
                # At end and just using final checkpoint.
                ckpt = _save()
                __update_and_write_run_info('best_ckpt', ckpt)
                logging.info("Last checkpoint {:}".format(ckpt))

            if args.save_all_ckpts:
                # Save at every checkpoint
                ckpt = _save()
                logging.info("Saving checkpoint {:}".format(ckpt))

            ## Save train and val results
            logging.info("Saving train and val results")
            train_df = pd.DataFrame(
                np.array([train_ensembles, train_labels, train_preds, train_logits]).T,
                columns=['ensembles', 'true', 'pred', 'logits'],
                )
            train_df.to_pickle(os.path.join(args.output_dir, 'train_result-{:}.pkl'.format(epoch)))

            val_df = pd.DataFrame(
                np.array([val_ensembles, val_labels, val_preds, val_logits]).T,
                columns=['ensembles', 'true', 'pred', 'logits'],
                )
            val_df.to_pickle(os.path.join(args.output_dir, 'val_result-{:}.pkl'.format(epoch)))

            __stats('Train Epoch {:}'.format(epoch), train_df)
            __stats('Val Epoch {:}'.format(epoch), val_df)

            if args.early_stopping and curr_val_loss >= prev_val_loss:
                logging.info("Validation loss stopped decreasing, stopping...")
                break
            else:
                prev_val_loss = curr_val_loss

        logging.info("Finished training")

    ##### Testing
    logging.debug("Run testing")
    if not args.test_only:
        to_use = run_info['best_ckpt'] if args.use_best else ckpt
    else:
        if args.use_ckpt_num == None:
            with open(os.path.join(args.model_dir, 'run_info.json')) as f:
                run_info = json.load(f)
            to_use = run_info['best_ckpt']
        else:
            to_use = os.path.join(
                args.model_dir, 'model-ckpt-{:}'.format(args.use_ckpt_num))
        saver = tf.train.import_meta_graph(to_use + '.meta')

    logging.info("Using {:} for testing".format(to_use))
    saver.restore(sess, to_use)

    test_generator_callable = functools.partial(
        feature_mut.dataset_generator,
        args.test_sharded,
        args.grid_config,
        shuffle=args.shuffle,
        repeat=args.repeat_gen,
        add_flag=args.add_flag,
        center_at_mut=args.center_at_mut,
        testing=True,
        random_seed=args.random_seed)

    test_num_ensembles = args.test_sharded.get_num_keyed()
    test_num_ensembles *= args.repeat_gen
    logging.info("Start testing with {:} ensembles".format(test_num_ensembles))

    test_ensembles, test_logits, test_preds, test_labels, _, test_loss = __loop(
        test_generator_callable, 'test', num_iters=test_num_ensembles)
    logging.info("Finished testing")

    test_df = pd.DataFrame(
        np.array([test_ensembles, test_labels, test_preds, test_logits]).T,
        columns=['ensembles', 'true', 'pred', 'logits'],
        )
    test_df.to_pickle(os.path.join(args.output_dir, 'test_result.pkl'))
    __stats('Test', test_df)


def create_train_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--train_sharded', type=str,
        default=os.environ['MUT_TRAIN_SHARDED'])
    parser.add_argument(
        '--val_sharded', type=str,
        default=os.environ['MUT_VAL_SHARDED'])
    parser.add_argument(
        '--test_sharded', type=str,
        default=os.environ['MUT_TEST_SHARDED'])

    parser.add_argument(
        '--output_dir', type=str,
        default=os.environ['MODEL_DIR'])

    # Training parameters
    parser.add_argument('--max_pos_per_shard', type=int, default=20)
    parser.add_argument('--neg_to_pos_ratio', type=float, default=1.0)

    parser.add_argument('--learning_rate', type=float, default=0.001)
    parser.add_argument('--conv_drop_rate', type=float, default=0.1)
    parser.add_argument('--fc_drop_rate', type=float, default=0.25)
    parser.add_argument('--top_nn_drop_rate', type=float, default=0.5)
    parser.add_argument('--top_nn_activation', type=str, default=None)
    parser.add_argument('--num_epochs', type=int, default=5)
    parser.add_argument('--repeat_gen', type=int, default=1)

    parser.add_argument('--add_flag', action='store_true', default=False)
    parser.add_argument('--center_at_mut', action='store_true', default=False)

    parser.add_argument('--num_conv', type=int, default=4)
    parser.add_argument('--num_final_fc_layers', type=int, default=2)
    parser.add_argument('--use_batch_norm', action='store_true', default=False)
    parser.add_argument('--no_dropout', action='store_true', default=False)

    parser.add_argument('--batch_size', type=int, default=4)
    parser.add_argument('--shuffle', action='store_true', default=False)
    parser.add_argument('--early_stopping', action='store_true', default=False)
    parser.add_argument('--use_best', action='store_true', default=False)
    parser.add_argument('--random_seed', type=int, default=random.randint(1, 10e6))
    parser.add_argument('--debug', action='store_true', default=False)
    parser.add_argument('--unobserved', action='store_true', default=False)
    parser.add_argument('--save_all_ckpts', action='store_true', default=False)

    # Test only
    parser.add_argument('--test_only', action='store_true', default=False)
    parser.add_argument('--model_dir', type=str, default=None)
    parser.add_argument('--use_ckpt_num', type=int, default=None)

    return parser


def main():
    parser = create_train_parser()
    args = parser.parse_args()

    log_fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format=log_fmt)
    else:
        logging.basicConfig(level=logging.INFO, format=log_fmt)
    logging.info("Running 3D CNN Mutation training...")

    if args.unobserved:
        args.output_dir = os.path.join(args.output_dir, 'None')
        os.makedirs(args.output_dir, exist_ok=True)
    else:
        num = 0
        while True:
            dirpath = os.path.join(args.output_dir, str(num))
            if os.path.exists(dirpath):
                num += 1
            else:
                args.output_dir = dirpath
                logging.info('Creating output directory {:}'.format(args.output_dir))
                os.mkdir(args.output_dir)
                break

    args.__dict__['grid_config'] = feature_mut.grid_config
    args.__dict__['grid_config'].neg_to_pos_ratio = args.neg_to_pos_ratio
    args.__dict__['grid_config'].max_pos_per_shard = args.max_pos_per_shard

    logging.info("\n" + str(json.dumps(args.__dict__, indent=4)) + "\n")

    # Save config
    with open(os.path.join(args.output_dir, 'config.json'), 'w') as f:
        json.dump(args.__dict__, f, indent=4)

    args.train_sharded = sh.Sharded.load(args.train_sharded)
    args.val_sharded = sh.Sharded.load(args.val_sharded)
    args.test_sharded = sh.Sharded.load(args.test_sharded)

    logging.info("Writing all output to {:}".format(args.output_dir))
    with tf.Session() as sess:
        np.random.seed(args.random_seed)
        tf.set_random_seed(args.random_seed)
        train_model(sess, args)


if __name__ == '__main__':
    main()
