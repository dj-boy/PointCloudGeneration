#!/usr/bin/python3
"""Training and Validation On Segmentation Task."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
import math
import random
import shutil
import argparse
import importlib
import data_utils
import numpy as np
import pointfly as pf
import tensorflow as tf
from datetime import datetime
from tqdm import tqdm
from simple_net import SimpleNet
import pdb

def main():
    #pdb.set_trace()
    parser = argparse.ArgumentParser()
    parser.add_argument('--filelist', '-t', help='Path to training set ground truth (.txt)', required=True)
    parser.add_argument('--filelist_val', '-v', help='Path to validation set ground truth (.txt)', required=True)
    parser.add_argument('--load_ckpt', '-l', help='Path to a check point file for load')
    parser.add_argument('--save_folder', '-s', help='Path to folder for saving check points and summary', required=True)
    parser.add_argument('--model', '-m', help='Model to use', required=True)
    parser.add_argument('--setting', '-x', help='Setting to use', required=True)
    parser.add_argument('--epochs', help='Number of training epochs (default defined in setting)', type=int)
    parser.add_argument('--batch_size', help='Batch size (default defined in setting)', type=int)
    parser.add_argument('--log', help='Log to FILE in save folder; use - for stdout (default is log.txt)', metavar='FILE', default='log.txt')
    parser.add_argument('--no_timestamp_folder', help='Dont save to timestamp folder', action='store_true')
    parser.add_argument('--no_code_backup', help='Dont backup code', action='store_true')
    args = parser.parse_args()

    if not args.no_timestamp_folder:
        time_string = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
        root_folder = os.path.join(args.save_folder, '%s_%s_%s_%d' % (args.model, args.setting, time_string, os.getpid()))
    else:
        root_folder = args.save_folder
    if not os.path.exists(root_folder):
        os.makedirs(root_folder)

    if args.log != '-':
        sys.stdout = open(os.path.join(root_folder, args.log), 'w')

    #print('PID:', os.getpid())

    #print(args)

    model = importlib.import_module(args.model)
    setting_path = os.path.join(os.path.dirname(__file__), args.model)
    sys.path.append(setting_path)
    setting = importlib.import_module(args.setting)

    num_epochs = args.epochs or setting.num_epochs
    batch_size = args.batch_size or setting.batch_size
    sample_num = setting.sample_num
    step_val = setting.step_val
    label_weights_list = setting.label_weights
    rotation_range = setting.rotation_range
    rotation_range_val = setting.rotation_range_val
    scaling_range = setting.scaling_range
    scaling_range_val = setting.scaling_range_val
    jitter = setting.jitter
    jitter_val = setting.jitter_val

    # Prepare inputs
    #print('{}-Preparing datasets...'.format(datetime.now()))
    is_list_of_h5_list = not data_utils.is_h5_list(args.filelist)
    if is_list_of_h5_list:
        seg_list = data_utils.load_seg_list(args.filelist)
        seg_list_idx = 0
        filelist_train = seg_list[seg_list_idx]
        seg_list_idx = seg_list_idx + 1
    else:
        filelist_train = args.filelist
    data_train, data_class, data_num_train, label_train, _ = data_utils.load_seg(filelist_train)
    data_val, data_val_class, data_num_val, label_val, _ = data_utils.load_seg(args.filelist_val)

    class_idx = 0
    data_train = data_train[data_class==class_idx]
    data_num_train = data_num_train[data_class==class_idx]
    label_train = label_train[data_class==class_idx]
    data_val = data_val[data_val_class==class_idx]
    data_num_val = data_num_val[data_val_class==class_idx]
    label_val = label_val[data_val_class==class_idx]
    # shuffle
    # data_num_train is the number of points in each point cloud
    data_train, data_num_train, label_train = \
        data_utils.grouped_shuffle([data_train, data_num_train, label_train])

    num_train = data_train.shape[0]
    point_num = data_train.shape[1]
    num_val = data_val.shape[0]
    #print('{}-{:d}/{:d} training/validation samples.'.format(datetime.now(), num_train, num_val))
    batch_num = (num_train * num_epochs + batch_size - 1) // batch_size
    #print('{}-{:d} training batches.'.format(datetime.now(), batch_num))
    batch_num_val = math.ceil(num_val / batch_size)
    #print('{}-{:d} testing batches per test.'.format(datetime.now(), batch_num_val))

    ######################################################################
    # Placeholders
    indices = tf.placeholder(tf.int32, shape=(None, None, 2), name="indices")
    xforms = tf.placeholder(tf.float32, shape=(None, 3, 3), name="xforms")
    rotations = tf.placeholder(tf.float32, shape=(None, 3, 3), name="rotations")
    jitter_range = tf.placeholder(tf.float32, shape=(1), name="jitter_range")
    global_step = tf.Variable(0, trainable=False, name='global_step')
    is_training = tf.placeholder(tf.bool, name='is_training')

    pts_fts = tf.placeholder(tf.float32, shape=(None, point_num, setting.data_dim), name='pts_fts')
    labels = tf.placeholder(tf.int32, shape=(None,), name='labels')
    sigma = tf.placeholder(tf.float32, shape=(None,), name='sigma')
    ######################################################################
    pts_fts_sampled = tf.gather_nd(pts_fts, indices=indices, name='pts_fts_sampled')
    features_augmented = None
    if setting.data_dim > 3:
        points_sampled, features_sampled = tf.split(pts_fts_sampled,
                                                    [3, setting.data_dim - 3],
                                                    axis=-1,
                                                    name='split_points_features')
        if setting.use_extra_features:
            if setting.with_normal_feature:
                if setting.data_dim < 6:
                    print('Only 3D normals are supported!')
                    exit()
                elif setting.data_dim == 6:
                    features_augmented = pf.augment(features_sampled, rotations)
                else:
                    normals, rest = tf.split(features_sampled, [3, setting.data_dim - 6])
                    normals_augmented = pf.augment(normals, rotations)
                    features_augmented = tf.concat([normals_augmented, rest], axis=-1)
            else:
                features_augmented = features_sampled
    else:
        points_sampled = pts_fts_sampled
    points_augmented = pf.augment(points_sampled, xforms, jitter_range)
    points_augmented = points_augmented - tf.reduce_min(points_augmented, axis=1, keepdims=True)
    points_augmented = points_augmented / tf.reduce_max(points_augmented, axis=1, keepdims=True)
    sigma_unsqueezed = tf.reshape(sigma, shape=(-1, 1, 1))
    perturbed_points = points_augmented + tf.random.normal(shape=tf.shape(points_augmented)) * sigma_unsqueezed
    net = model.Net(perturbed_points, features_augmented, labels, is_training, setting)
    scores = net.logits
    #net = SimpleNet()
    #scores = net.forward(perturbed_points, labels)
    target = - 1 / (sigma_unsqueezed ** 2) * (perturbed_points - points_augmented)
    target = tf.reshape(target, shape=[tf.shape(target)[0],-1])
    scores = tf.reshape(scores, shape=[tf.shape(scores)[0],-1])
    loss_op = tf.reduce_sum(0.5 * (target - scores) ** 2, axis=-1) * sigma ** 2.0

    with tf.name_scope('metrics'):
        loss_mean_op, loss_mean_update_op = tf.metrics.mean(loss_op)
    reset_metrics_op = tf.variables_initializer([var for var in tf.local_variables()
                                                 if var.name.split('/')[0] == 'metrics'])


    _ = tf.summary.scalar('loss/train', tensor=loss_mean_op, collections=['train'])
    _ = tf.summary.scalar('loss/val', tensor=loss_mean_op, collections=['val'])

    lr_exp_op = tf.train.exponential_decay(setting.learning_rate_base, global_step, setting.decay_steps,
                                           setting.decay_rate, staircase=True)
    lr_clip_op = tf.maximum(lr_exp_op, setting.learning_rate_min)
    _ = tf.summary.scalar('learning_rate', tensor=lr_clip_op, collections=['train'])
    reg_loss = setting.weight_decay * tf.losses.get_regularization_loss()
    if setting.optimizer == 'adam':
        optimizer = tf.train.AdamOptimizer(learning_rate=lr_clip_op, epsilon=setting.epsilon)
    elif setting.optimizer == 'momentum':
        optimizer = tf.train.MomentumOptimizer(learning_rate=lr_clip_op, momentum=setting.momentum, use_nesterov=True)
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops):
        train_op = optimizer.minimize(loss_op + reg_loss, global_step=global_step)

    init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())

    saver = tf.train.Saver(max_to_keep=None)

    # backup all code
    if not args.no_code_backup:
        code_folder = os.path.abspath(os.path.dirname(__file__))
        shutil.copytree(code_folder, os.path.join(root_folder, os.path.basename(code_folder)))

    folder_ckpt = os.path.join(root_folder, 'ckpts')
    if not os.path.exists(folder_ckpt):
        os.makedirs(folder_ckpt)

    folder_summary = os.path.join(root_folder, 'summary')
    if not os.path.exists(folder_summary):
        os.makedirs(folder_summary)

    parameter_num = np.sum([np.prod(v.shape.as_list()) for v in tf.trainable_variables()])
    print('{}-Parameter number: {:d}.'.format(datetime.now(), parameter_num))

    sigmas = np.exp(np.linspace(np.log(1.0), np.log(0.01), 10))
    label_range = np.arange(10)
    best_val_loss = 1e7
    with tf.Session() as sess:
        summaries_op = tf.summary.merge_all('train')
        summaries_val_op = tf.summary.merge_all('val')
        summary_writer = tf.summary.FileWriter(folder_summary, sess.graph)

        sess.run(init_op)

        # Load the model
        if args.load_ckpt is not None:
            saver.restore(sess, tf.train.latest_checkpoint(args.load_ckpt))
            print('{}-Checkpoint loaded from {}!'.format(datetime.now(), args.load_ckpt))
        else:
            latest_ckpt = tf.train.latest_checkpoint(folder_ckpt)
            if latest_ckpt:
                print('{}-Found checkpoint {}'.format(datetime.now(), latest_ckpt))
                saver.restore(sess, latest_ckpt)
                print('{}-Checkpoint loaded from {} (Iter {})'.format(
                    datetime.now(), latest_ckpt, sess.run(global_step)))

        for batch_idx_train in tqdm(range(batch_num)):
            ######################################################################
            # Validation
            
            if (batch_idx_train % step_val == 0 and (batch_idx_train != 0 or args.load_ckpt is not None)) \
                    or batch_idx_train == batch_num - 1:
                filename_ckpt = os.path.join(folder_ckpt, 'iter')
                print('{}-Checkpoint saved to {}!'.format(datetime.now(), filename_ckpt))

                sess.run(reset_metrics_op)
                for batch_val_idx in range(batch_num_val):
                    start_idx = batch_size * batch_val_idx
                    end_idx = min(start_idx + batch_size, num_val)
                    batch_size_val = end_idx - start_idx
                    points_batch = data_val[start_idx:end_idx, ...]
                    points_num_batch = data_num_val[start_idx:end_idx, ...]
                    label_used = np.random.choice(label_range, batch_size_val)
                    sigma_used = sigmas[label_used]

                    xforms_np, rotations_np = pf.get_xforms(batch_size_val,
                                                            rotation_range=rotation_range_val,
                                                            scaling_range=scaling_range_val,
                                                            order=setting.rotation_order)

                    
                    sess.run([loss_mean_update_op],
                             feed_dict={
                                 pts_fts: points_batch,
                                 indices: pf.get_indices(batch_size_val, sample_num, points_num_batch),
                                 xforms: xforms_np,
                                 rotations: rotations_np,
                                 jitter_range: np.array([jitter_val]),
                                 is_training: False,
                                 labels: label_used,
                                 sigma: sigma_used,
                             })
                loss_val, summaries_val, step = sess.run(
                    [loss_mean_op, summaries_val_op, global_step])
                summary_writer.add_summary(summaries_val, step)
                if loss_val < best_val_loss:
                    best_val_loss = loss_val
                    saver.save(sess, filename_ckpt, global_step=global_step)
                print('{}-[Val  ]-Average:      Loss: {:.4f}'
                      .format(datetime.now(), loss_val))
                sys.stdout.flush()
            ######################################################################

            ######################################################################
            # Training
            start_idx = (batch_size * batch_idx_train) % num_train
            end_idx = min(start_idx + batch_size, num_train)
            batch_size_train = end_idx - start_idx
            points_batch = data_train[start_idx:end_idx, ...]
            print(points_batch.shape)
            points_num_batch = data_num_train[start_idx:end_idx, ...]

            if start_idx + batch_size_train == num_train:
                
                if is_list_of_h5_list:
                    filelist_train_prev = seg_list[(seg_list_idx - 1) % len(seg_list)]
                    filelist_train = seg_list[seg_list_idx % len(seg_list)]
                    if filelist_train != filelist_train_prev:
                        data_train, data_class, data_num_train, label_train, _ = data_utils.load_seg(filelist_train)
                        data_train = data_train[data_class==class_idx]
                        data_num_train = data_num_train[data_class==class_idx]
                        label_train = label_train[data_class==class_idx]
                        num_train = data_train.shape[0]
                    seg_list_idx = seg_list_idx + 1
                data_train, data_num_train, label_train = \
                    data_utils.grouped_shuffle([data_train, data_num_train, label_train])

            offset = int(random.gauss(0, sample_num * setting.sample_num_variance))
            offset = max(offset, -sample_num * setting.sample_num_clip)
            offset = min(offset, sample_num * setting.sample_num_clip)
            sample_num_train = sample_num + offset
            xforms_np, rotations_np = pf.get_xforms(batch_size_train,
                                                    rotation_range=rotation_range,
                                                    scaling_range=scaling_range,
                                                    order=setting.rotation_order)
            
            label_used = np.random.choice(label_range, batch_size_train)
            sigma_used = sigmas[label_used]

            sess.run(reset_metrics_op)
            sess.run([train_op, loss_mean_update_op],
                     feed_dict={
                         pts_fts: points_batch,
                         indices: pf.get_indices(batch_size_train, sample_num_train, points_num_batch),
                         xforms: xforms_np,
                         rotations: rotations_np,
                         jitter_range: np.array([jitter]),
                         is_training: True,
                         labels: label_used,
                         sigma: sigma_used,
                     })
            if batch_idx_train % 10 == 0:
                loss, summaries, step = sess.run([loss_mean_op,
                                                summaries_op,
                                                global_step])
                summary_writer.add_summary(summaries, step)
                print('{}-[Train]-Iter: {:06d}  Loss: {:.4f}'
                      .format(datetime.now(), step, loss))
                sys.stdout.flush()
            ######################################################################
        print('{}-Done!'.format(datetime.now()))

if __name__ == '__main__':
    main()
