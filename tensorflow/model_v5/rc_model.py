# -*- coding:utf8 -*-
# ==============================================================================
# Copyright 2017 Baidu.com, Inc. All Rights Reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""
This module implements the reading comprehension models based on:
1. the BiDAF algorithm described in https://arxiv.org/abs/1611.01603
2. the Match-LSTM algorithm described in https://openreview.net/pdf?id=B1-q5Pqxl
Note that we use Pointer Network for the decoding stage of both models.
"""

import os
import time
import logging
import json
import numpy as np
import tensorflow as tf
import keras.backend as K
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
from utils import compute_bleu_rouge
from utils import normalize
from layers.basic_rnn import rnn, cudnn_rnn, bilstm, bilstm_layer
from layers.match_layer import MatchLSTMLayer
from layers.match_layer import AttentionFlowMatchLayer
from layers.pointer_net import PointerNetDecoder


class RCModel(object):
    """
    Implements the main reading comprehension model.
    """

    def __init__(self, vocab, args):

        # logging
        self.logger = logging.getLogger("brc")

        # basic config
        self.algo = args.algo
        self.hidden_size = args.hidden_size
        self.optim_type = args.optim
        self.learning_rate = args.learning_rate
        self.weight_decay = args.weight_decay
        self.use_dropout = args.dropout_keep_prob < 1

        # length limit
        self.max_p_num = args.max_p_num
        self.max_p_len = args.max_p_len
        self.max_q_len = args.max_q_len
        self.max_a_len = args.max_a_len

        # the vocab
        self.vocab = vocab

        # session info
        sess_config = tf.ConfigProto()
        sess_config.gpu_options.allow_growth = True
        self.sess = tf.Session(config=sess_config)
        K.set_session(self.sess)

        self._build_graph()

        # save info
        self.saver = tf.train.Saver()

        # initialize the model
        self.sess.run(tf.global_variables_initializer())

    def _build_graph(self):
        """
        Builds the computation graph with Tensorflow
        """
        start_t = time.time()
        self._setup_placeholders()
        self._embed()
        self._encode()
        self._match()
        self._fuse()
        
        if self.algo=='BIDAF':
            self._decode_qa()
            self._compute_loss_qa()
        elif self.algo=='YESNO':
            self._decode_yesno()
            self._compute_loss_yesno()
        else:
            raise NotImplementedError('No algo named as {}'.format(self.algo))
        
        self._create_train_op()
        self.logger.info('Time to build graph: {} s'.format(time.time() - start_t))
        param_num = sum([np.prod(self.sess.run(tf.shape(v))) for v in self.all_params])
        self.logger.info('There are {} parameters in the model'.format(param_num))

    def _setup_placeholders(self):
        """
        Placeholders
        """
        self.p = tf.placeholder(tf.int32, [None, None])
        self.q = tf.placeholder(tf.int32, [None, None])
        self.p_length = tf.placeholder(tf.int32, [None])
        self.q_length = tf.placeholder(tf.int32, [None])

        if self.algo=='BIDAF':
            self.start_label = tf.placeholder(tf.int32, [None])
            self.end_label = tf.placeholder(tf.int32, [None])
        elif self.algo=='YESNO':
            self.yesno_label = tf.placeholder(tf.int32, [None])
        else:
            raise NotImplementedError('No algo named as {}'.format(self.algo))
        
        self.dropout_keep_prob = tf.placeholder(tf.float32)

    def _embed(self):
        """
        The embedding layer, question and passage share embeddings
        """
        with tf.device('/cpu:0'), tf.variable_scope('word_embedding'):
            self.word_embeddings = tf.get_variable(
                'word_embeddings',
                shape=(self.vocab.size(), self.vocab.embed_dim),
                initializer=tf.constant_initializer(self.vocab.embeddings),
                trainable=True
            )
            self.p_emb = tf.nn.embedding_lookup(self.word_embeddings, self.p)
            self.q_emb = tf.nn.embedding_lookup(self.word_embeddings, self.q)

    def _encode(self):
        """
        Employs two Bi-LSTMs to encode passage and question separately
        """
        if self.use_dropout:
            self.p_emb = tf.nn.dropout(self.p_emb, self.dropout_keep_prob)
            self.q_emb = tf.nn.dropout(self.q_emb, self.dropout_keep_prob)

        with tf.variable_scope('passage_encoding'):
            self.sep_p_encodes, _ = bilstm_layer(self.p_emb, self.p_length, self.hidden_size)
        with tf.variable_scope('question_encoding'):
            self.sep_q_encodes, _ = bilstm_layer(self.q_emb, self.q_length, self.hidden_size)

    def _match(self):
        """
        The core of RC model, get the question-aware passage encoding with either BIDAF or MLSTM
        """
        if self.use_dropout:
            self.sep_p_encodes = tf.nn.dropout(self.sep_p_encodes, self.dropout_keep_prob)
            self.sep_q_encodes = tf.nn.dropout(self.sep_q_encodes, self.dropout_keep_prob)

        if self.algo in ['BIDAF','YESNO']:
            match_layer = AttentionFlowMatchLayer(self.hidden_size)
        else:
            raise NotImplementedError('The algorithm {} is not implemented.'.format(self.algo))
        self.match_p_encodes, _ = match_layer.match(self.sep_p_encodes, self.sep_q_encodes, self.hidden_size)

    def _fuse(self):
        """
        Employs Bi-LSTM again to fuse the context information after match layer
        """
        with tf.variable_scope('fusion'):
            self.match_p_encodes = tf.layers.dense(self.match_p_encodes, self.hidden_size * 2,
                                                   activation=tf.nn.relu)

            self.residual_p_emb = self.match_p_encodes
            if self.use_dropout:
                self.residual_p_emb = tf.nn.dropout(self.match_p_encodes, self.dropout_keep_prob)

            self.residual_p_encodes, _ = bilstm_layer(self.residual_p_emb, self.p_length,
                                             self.hidden_size, layer_num=1)
            if self.use_dropout:
                self.residual_p_encodes = tf.nn.dropout(self.residual_p_encodes, self.dropout_keep_prob)
            #bilstm不能直接连接dense AttributeError: 'Bidirectional' object has no attribute 'outbound_nodes'
            sim_weight_1 = tf.get_variable("sim_weight_1", self.hidden_size * 2)
            weight_passage_encodes = self.residual_p_encodes * sim_weight_1
            dot_sim_matrix = tf.matmul(weight_passage_encodes, self.residual_p_encodes, transpose_b=True)
            sim_weight_2 = tf.get_variable("sim_weight_2", self.hidden_size * 2)
            passage_sim = tf.tensordot(self.residual_p_encodes, sim_weight_2, axes=[[2], [0]])
            sim_weight_3 = tf.get_variable("sim_weight_3", self.hidden_size * 2)
            question_sim = tf.tensordot(self.residual_p_encodes, sim_weight_3, axes=[[2], [0]])
            sim_matrix = dot_sim_matrix + tf.expand_dims(passage_sim, 2) + tf.expand_dims(question_sim, 1)
            # sim_matrix = tf.matmul(self.residual_p_encodes, self.residual_p_encodes, transpose_b=True)

            batch_size, num_rows = tf.shape(sim_matrix)[0:1], tf.shape(sim_matrix)[1]
            mask = tf.eye(num_rows, batch_shape=batch_size)
            sim_matrix = sim_matrix + -1e9 * mask

            context2question_attn = tf.matmul(tf.nn.softmax(sim_matrix, -1), self.residual_p_encodes)
            concat_outputs = tf.concat([self.residual_p_encodes, context2question_attn,
                                        self.residual_p_encodes * context2question_attn], -1)
            self.residual_match_p_encodes = tf.layers.dense(concat_outputs, self.hidden_size * 2, activation=tf.nn.relu)

            self.match_p_encodes = tf.add(self.match_p_encodes, self.residual_match_p_encodes)
            if self.use_dropout:
                self.match_p_encodes = tf.nn.dropout(self.match_p_encodes, self.dropout_keep_prob)

    def _decode_qa(self):
        """
        Employs Pointer Network to get the the probs of each position
        to be the start or end of the predicted answer.
        Note that we concat the fuse_p_encodes for the passages in the same document.
        And since the encodes of queries in the same document is same, we select the first one.
        """
        with tf.variable_scope('start_pos_predict'):
            self.fuse_p_encodes, _ = bilstm_layer(self.match_p_encodes, self.p_length,
                                         self.hidden_size, layer_num=1)
            start_weight = tf.get_variable("start_weight", self.hidden_size * 2)
            start_logits = tf.tensordot(self.fuse_p_encodes, start_weight, axes=[[2], [0]])

        with tf.variable_scope('end_pos_predict'):
            concat_GM_2 = tf.concat([self.match_p_encodes, self.fuse_p_encodes], -1)
            self.end_p_encodes, _ = bilstm_layer(concat_GM_2, self.p_length,
                                        self.hidden_size, layer_num=1)
            
            end_weight = tf.get_variable("start_weight", self.hidden_size * 2)
            end_logits = tf.tensordot(self.end_p_encodes, end_weight, axes=[[2], [0]])

        with tf.variable_scope('same_question_concat'):
            batch_size = tf.shape(self.start_label)[0]

            concat_start_logits = tf.reshape(start_logits, [batch_size, -1])
            concat_end_logits = tf.reshape(end_logits, [batch_size, -1])

        self.start_probs = tf.nn.softmax(concat_start_logits, axis=1)
        self.end_probs = tf.nn.softmax(concat_end_logits, axis=1)

    def _decode_yesno(self):
        """
        Employs Pointer Network to get the the probs of each position
        to be the start or end of the predicted answer.
        Note that we concat the fuse_p_encodes for the passages in the same document.
        And since the encodes of queries in the same document is same, we select the first one.
        """
        with tf.variable_scope('class_predict'):
            self.fuse_p_encodes, _ = bilstm_layer(self.match_p_encodes, self.p_length,
                                         self.hidden_size, layer_num=1)
            fuse_p_encodes_pool = tf.reduce_max(self.fuse_p_encodes, axis=1)#TODO--self.p_length作为mask?
            classify_weight=tf.get_variable("classify_weight",shape=[self.hidden_size * 2,3],dtype=tf.float32,initializer=tf.truncated_normal_initializer(stddev=0.1))
            bais=tf.get_variable("bais",shape=[3],dtype=tf.float32,initializer=tf.constant_initializer(0))
            class_logits = tf.nn.bias_add(tf.matmul(fuse_p_encodes_pool,classify_weight),bais)
        
        self.classprobs = tf.nn.softmax(class_logits, axis=1)

    def _compute_loss_qa(self):
        """
        The loss function
        """
        def sparse_nll_loss(probs, labels, epsilon=1e-9, scope=None):
            """
            negative log likelihood loss
            """

            with tf.name_scope(scope, "log_loss"):
                labels = tf.one_hot(labels, tf.shape(probs)[1], axis=1)
                losses = - tf.reduce_sum(labels * tf.log(probs + epsilon), 1)
            return losses

        self.start_loss = sparse_nll_loss(probs=self.start_probs, labels=self.start_label)
        self.end_loss = sparse_nll_loss(probs=self.end_probs, labels=self.end_label)
        self.all_params = tf.trainable_variables()
        self.loss = tf.reduce_mean(tf.add(self.start_loss, self.end_loss))
        if self.weight_decay > 0:
            with tf.variable_scope('l2_loss'):
                l2_loss = tf.add_n([tf.nn.l2_loss(v) for v in self.all_params])
            self.loss += self.weight_decay * l2_loss

    def _compute_loss_yesno(self):
        """
        The loss function
        """
        labels = tf.one_hot(self.yesno_label, tf.shape(self.classprobs)[1], axis=1)
        self.loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(labels=labels,logits=self.classprobs))#TODO
        self.all_params = tf.trainable_variables()
        if self.weight_decay > 0:
            with tf.variable_scope('l2_loss'):
                l2_loss = tf.add_n([tf.nn.l2_loss(v) for v in self.all_params])
            self.loss += self.weight_decay * l2_loss

    def _create_train_op(self):
        """
        Selects the training algorithm and creates a train operation with it
        """
        if self.optim_type == 'adagrad':
            self.optimizer = tf.train.AdagradOptimizer(self.learning_rate)
        elif self.optim_type == 'adam':
            self.optimizer = tf.train.AdamOptimizer(self.learning_rate)
        elif self.optim_type == 'rprop':
            self.optimizer = tf.train.RMSPropOptimizer(self.learning_rate)
        elif self.optim_type == 'sgd':
            self.optimizer = tf.train.GradientDescentOptimizer(self.learning_rate)
        else:
            raise NotImplementedError('Unsupported optimizer: {}'.format(self.optim_type))
        self.train_op = self.optimizer.minimize(self.loss)

    def _train_epoch_qa(self, train_batches, dropout_keep_prob):
        """
        Trains the model for a single epoch.
        Args:
            train_batches: iterable batch data for training
            dropout_keep_prob: float value indicating dropout keep probability
        """
        total_num, total_loss = 0, 0
        log_every_n_batch, n_batch_loss = 50, 0
        for bitx, batch in enumerate(train_batches, 1):
            feed_dict = {self.p: batch['passage_token_ids'],
                         self.q: batch['question_token_ids'],
                         self.p_length: batch['passage_length'],
                         self.q_length: batch['question_length'],
                         self.start_label: batch['start_id'],
                         self.end_label: batch['end_id'],
                         self.dropout_keep_prob: dropout_keep_prob}
            _, loss = self.sess.run([tf.shape(self.sep_p_encodes), tf.shape(self.sep_q_encodes)], feed_dict)
            _, loss = self.sess.run([self.train_op, self.loss], feed_dict)
            total_loss += loss * len(batch['raw_data'])
            total_num += len(batch['raw_data'])
            n_batch_loss += loss
            if log_every_n_batch > 0 and bitx % log_every_n_batch == 0:
                self.logger.info('Average loss from batch {} to {} is {}'.format(
                    bitx - log_every_n_batch + 1, bitx, n_batch_loss / log_every_n_batch))
                n_batch_loss = 0
        return 1.0 * total_loss / total_num

    def _train_epoch_yesno(self, train_batches, dropout_keep_prob):
        """
        Trains the model for a single epoch.
        Args:
            train_batches: iterable batch data for training
            dropout_keep_prob: float value indicating dropout keep probability
        """
        total_num, total_loss = 0, 0
        log_every_n_batch, n_batch_loss = 50, 0
        for bitx, batch in enumerate(train_batches, 1):
            feed_dict = {self.p: batch['passage_token_ids'],
                         self.q: batch['question_token_ids'],
                         self.p_length: batch['passage_length'],
                         self.q_length: batch['question_length'],
                         self.yesno_label: batch['yesno_label'],
                         self.dropout_keep_prob: dropout_keep_prob}
            _, loss = self.sess.run([tf.shape(self.sep_p_encodes), tf.shape(self.sep_q_encodes)], feed_dict)
            _, loss = self.sess.run([self.train_op, self.loss], feed_dict)
            total_loss += loss * len(batch['raw_data'])
            total_num += len(batch['raw_data'])
            n_batch_loss += loss
            if log_every_n_batch > 0 and bitx % log_every_n_batch == 0:
                self.logger.info('Average loss from batch {} to {} is {}'.format(
                    bitx - log_every_n_batch + 1, bitx, n_batch_loss / log_every_n_batch))
                n_batch_loss = 0
        return 1.0 * total_loss / total_num

    def train(self, data, epochs, batch_size, save_dir, save_prefix,
              dropout_keep_prob=1.0, evaluate=True):
        if self.algo=='BIDAF':
            self._train_qa(data, epochs, batch_size, save_dir, save_prefix, dropout_keep_prob, evaluate)
        elif self.algo=='YESNO':
            self._train_yesno(data, epochs, batch_size, save_dir, save_prefix, dropout_keep_prob, evaluate)
        else:
            raise NotImplementedError('No algo named as {}'.format(self.algo))

    def _train_qa(self, data, epochs, batch_size, save_dir, save_prefix,
              dropout_keep_prob=1.0, evaluate=True):
        """
        Train the model with data
        Args:
            data: the BRCDataset class implemented in dataset.py
            epochs: number of training epochs
            batch_size:
            save_dir: the directory to save the model
            save_prefix: the prefix indicating the model type
            dropout_keep_prob: float value indicating dropout keep probability
            evaluate: whether to evaluate the model on test set after each epoch
        """
        pad_id = self.vocab.get_id(self.vocab.pad_token)
        # max_bleu_4 = 0
        max_rougeL = 0
        for epoch in range(1, epochs + 1):
            self.logger.info('Training the model for epoch {}'.format(epoch))
            train_batches = data.gen_mini_batches('train', batch_size, pad_id, shuffle=True)
            train_loss = self._train_epoch_qa(train_batches, dropout_keep_prob)
            self.logger.info('Average train loss for epoch {} is {}'.format(epoch, train_loss))

            if evaluate:
                self.logger.info('Evaluating the model after epoch {}'.format(epoch))
                if data.dev_set is not None:
                    eval_batches = data.gen_mini_batches('dev', batch_size, pad_id, shuffle=False)
                    eval_loss, bleu_rouge = self.evaluate(eval_batches)
                    self.logger.info('Dev eval loss {}'.format(eval_loss))
                    self.logger.info('Dev eval result: {}'.format(bleu_rouge))

                    # if bleu_rouge['Bleu-4'] > max_bleu_4:
                    #     self.save(save_dir, save_prefix)
                    #     max_bleu_4 = bleu_rouge['Bleu-4']

                    if bleu_rouge['Rouge-L'] > max_rougeL:
                        self.save(save_dir, save_prefix)
                        max_rougeL = bleu_rouge['Rouge-L'] 
                else:
                    self.logger.warning('No dev set is loaded for evaluation in the dataset!')
            else:
                self.save(save_dir, save_prefix + '_' + str(epoch))

    def _train_yesno(self, data, epochs, batch_size, save_dir, save_prefix,
              dropout_keep_prob=1.0, evaluate=True):
        """
        Train the model with data
        Args:
            data: the BRCDataset class implemented in dataset.py
            epochs: number of training epochs
            batch_size:
            save_dir: the directory to save the model
            save_prefix: the prefix indicating the model type
            dropout_keep_prob: float value indicating dropout keep probability
            evaluate: whether to evaluate the model on test set after each epoch
        """
        pad_id = self.vocab.get_id(self.vocab.pad_token)
        max_acc = 0
        for epoch in range(1, epochs + 1):
            self.logger.info('Training the model for epoch {}'.format(epoch))
            train_batches = data.gen_mini_batches('train', batch_size, pad_id, shuffle=True)
            train_loss = self._train_epoch_yesno(train_batches, dropout_keep_prob)
            self.logger.info('Average train loss for epoch {} is {}'.format(epoch, train_loss))

            if evaluate:
                self.logger.info('Evaluating the model after epoch {}'.format(epoch))
                if data.dev_set is not None:
                    eval_batches = data.gen_mini_batches('dev', batch_size, pad_id, shuffle=False)
                    eval_loss, acc_p_r_f1 = self.evaluate(eval_batches)
                    self.logger.info('Dev eval loss {}'.format(eval_loss))
                    self.logger.info('Dev eval result: {}'.format(acc_p_r_f1))

                    if acc_p_r_f1['accuracy'] > max_acc:
                        self.save(save_dir, save_prefix)
                        max_acc = acc_p_r_f1['accuracy']
                else:
                    self.logger.warning('No dev set is loaded for evaluation in the dataset!')
            else:
                self.save(save_dir, save_prefix + '_' + str(epoch))

    def evaluate(self, eval_batches, result_dir=None, result_prefix=None, save_full_info=False):
        if self.algo=='BIDAF':
            return self._evaluate_qa(eval_batches, result_dir, result_prefix, save_full_info)
        elif self.algo=='YESNO':
            return self._evaluate_yesno(eval_batches, result_dir, result_prefix, save_full_info)
        else:
            raise NotImplementedError('No algo named as {}'.format(self.algo))

    def _evaluate_qa(self, eval_batches, result_dir=None, result_prefix=None, save_full_info=False):
        """
        Evaluates the model performance on eval_batches and results are saved if specified
        Args:
            eval_batches: iterable batch data
            result_dir: directory to save predicted answers, answers will not be saved if None
            result_prefix: prefix of the file for saving predicted answers,
                           answers will not be saved if None
            save_full_info: if True, the pred_answers will be added to raw sample and saved
        """
        pred_answers, ref_answers = [], []
        total_loss, total_num = 0, 0
        for b_itx, batch in enumerate(eval_batches):
            feed_dict = {self.p: batch['passage_token_ids'],
                         self.q: batch['question_token_ids'],
                         self.p_length: batch['passage_length'],
                         self.q_length: batch['question_length'],
                         self.start_label: batch['start_id'],
                         self.end_label: batch['end_id'],
                         self.dropout_keep_prob: 1.0}
            # print(self.sess.run([tf.shape(self.match_p_encodes)], feed_dict))
            start_probs, end_probs, loss = self.sess.run([self.start_probs,
                                                          self.end_probs, self.loss], feed_dict)

            total_loss += loss * len(batch['raw_data'])
            total_num += len(batch['raw_data'])

            padded_p_len = len(batch['passage_token_ids'][0])
            for sample, start_prob, end_prob in zip(batch['raw_data'], start_probs, end_probs):

                best_answer,segmented_answer = self.find_best_answer(sample, start_prob, end_prob, padded_p_len)
                if save_full_info:
                    sample['pred_answers'] = [best_answer]
                    pred_answers.append(sample)
                else:
                    if sample['question_type']=='YES_NO':
                        pred_answers.append({'question_id': sample['question_id'],
                                             'question_type': sample['question_type'],
                                             'segmented_question': sample['segmented_question'],
                                             'answers': [best_answer],
                                             'segmented_answers': segmented_answer,#TODO
                                             'entity_answers': [[]],
                                             'yesno_answers': []})
                    else:
                        pred_answers.append({'question_id': sample['question_id'],
                                             'question_type': sample['question_type'],
                                             'answers': [best_answer],
                                             'entity_answers': [[]],
                                             'yesno_answers': []})
                if 'answers' in sample:
                    ref_answers.append({'question_id': sample['question_id'],
                                        'question_type': sample['question_type'],
                                        'answers': sample['answers'],
                                        'entity_answers': [[]],
                                        'yesno_answers': []})

        if result_dir is not None and result_prefix is not None:
            result_file = os.path.join(result_dir, result_prefix + '.json')
            with open(result_file, 'w') as fout:
                for pred_answer in pred_answers:
                    fout.write(json.dumps(pred_answer, ensure_ascii=False) + '\n')

            self.logger.info('Saving {} results to {}'.format(result_prefix, result_file))

        # this average loss is invalid on test set, since we don't have true start_id and end_id
        ave_loss = 1.0 * total_loss / total_num
        # compute the bleu and rouge scores if reference answers is provided
        if len(ref_answers) > 0:
            pred_dict, ref_dict = {}, {}
            for pred, ref in zip(pred_answers, ref_answers):
                question_id = ref['question_id']
                if len(ref['answers']) > 0:
                    pred_dict[question_id] = normalize(pred['answers'])
                    ref_dict[question_id] = normalize(ref['answers'])
            bleu_rouge = compute_bleu_rouge(pred_dict, ref_dict)
        else:
            bleu_rouge = None
        return ave_loss, bleu_rouge

    def _evaluate_yesno(self, eval_batches, result_dir=None, result_prefix=None, save_full_info=False):
        """
        Evaluates the model performance on eval_batches and results are saved if specified
        Args:
            eval_batches: iterable batch data
            result_dir: directory to save predicted answers, answers will not be saved if None
            result_prefix: prefix of the file for saving predicted answers,
                           answers will not be saved if None
            save_full_info: if True, the pred_answers will be added to raw sample and saved
        """
        labels = {'Yes':0, 'No':1, 'Depends':2}
        idx2labels = {v : k for k, v in labels.items()}
        pred_answers = []
        pred_labels, ref_labels = [], []
        total_loss, total_num = 0, 0
        for b_itx, batch in enumerate(eval_batches):
            feed_dict = {self.p: batch['passage_token_ids'],
                         self.q: batch['question_token_ids'],
                         self.p_length: batch['passage_length'],
                         self.q_length: batch['question_length'],
                         self.yesno_label: batch['yesno_label'],
                         self.dropout_keep_prob: 1.0}
            # print(self.sess.run([tf.shape(self.match_p_encodes)], feed_dict))
            class_probs, loss = self.sess.run([self.classprobs,self.loss], feed_dict)

            total_loss += loss * len(batch['raw_data'])
            total_num += len(batch['raw_data'])

            # predicted = tf.cast(tf.arg_max(class_probs, 1), tf.int32)#选择预测输出值最大概率类别
            predicted = np.argmax(class_probs, 1)
            pred_labels.extend(predicted)
            ref_labels.extend(batch['yesno_label'])

            for sample, pred in zip(batch['raw_data'], predicted):
                if save_full_info:# True for dev
                    sample['pred_class'] = [idx2labels[pred]]
                    pred_answers.append(sample)
                else:
                    pred_answers.append({'question_id': sample['question_id'],
                                         'question_type': sample['question_type'],
                                         'answers': sample['answers'],
                                         'entity_answers': [[]],
                                         'yesno_answers': [idx2labels[pred]]})

        if result_dir is not None and result_prefix is not None:
            result_file = os.path.join(result_dir, result_prefix + '.YESNO.json')
            with open(result_file, 'w') as fout:
                for pred_answer in pred_answers:
                    fout.write(json.dumps(pred_answer, ensure_ascii=False) + '\n')

            self.logger.info('Saving {} results to {}'.format(result_prefix, result_file))
        
        # this average loss is invalid on test set, since we don't have true yesno labels
        ave_loss = 1.0 * total_loss / total_num
        # compute the acc, p, r, f1 scores if reference answers is provided
        acc = accuracy_score(ref_labels, pred_labels)
        p = precision_score(ref_labels, pred_labels, average='macro')  
        r = recall_score(ref_labels, pred_labels, average='macro')
        f1 = f1_score(ref_labels, pred_labels, average='macro')
        acc_p_r_f1 = {'accuracy':acc, 'precision':p, 'recall':r ,'f1':f1}
        return ave_loss, acc_p_r_f1

    def find_best_answer(self, sample, start_prob, end_prob, padded_p_len):
        """
        Finds the best answer for a sample given start_prob and end_prob for each position.
        This will call find_best_answer_for_passage because there are multiple passages in a sample
        """
        best_p_idx, best_span, best_score = None, None, 0

        for p_idx, passage in enumerate(sample['passages']):
            if p_idx >= self.max_p_num:
                continue
            passage_len = min(self.max_p_len, len(passage['passage_tokens']))
            answer_span, score = self.find_best_answer_for_passage(
                start_prob[p_idx * padded_p_len: (p_idx + 1) * padded_p_len],
                end_prob[p_idx * padded_p_len: (p_idx + 1) * padded_p_len],
                passage_len)
            if score > best_score:
                best_score = score
                best_p_idx = p_idx
                best_span = answer_span
        if best_p_idx is None or best_span is None:
            best_answer = ''
            segmented_answer=[]
        else:
            best_answer = ''.join(
                sample['passages'][best_p_idx]['passage_tokens'][best_span[0]: best_span[1] + 1])
            segmented_answer=sample['passages'][best_p_idx]['passage_tokens'][best_span[0]: best_span[1] + 1]
        return best_answer, segmented_answer

    def find_best_answer_for_passage(self, start_probs, end_probs, passage_len=None):
        """
        Finds the best answer with the maximum start_prob * end_prob from a single passage
        """
        if passage_len is None:
            passage_len = len(start_probs)
        else:
            passage_len = min(len(start_probs), passage_len)
        best_start, best_end, max_prob = -1, -1, 0
        for start_idx in range(passage_len):
            for ans_len in range(self.max_a_len):
                end_idx = start_idx + ans_len
                if end_idx >= passage_len:
                    continue
                prob = start_probs[start_idx] * end_probs[end_idx]
                if prob > max_prob:
                    best_start = start_idx
                    best_end = end_idx
                    max_prob = prob
        return (best_start, best_end), max_prob

    def save(self, model_dir, model_prefix):
        """
        Saves the model into model_dir with model_prefix as the model indicator
        """
        self.saver.save(self.sess, os.path.join(model_dir, model_prefix))
        self.logger.info('Model saved in {}, with prefix {}.'.format(model_dir, model_prefix))

    def restore(self, model_dir, model_prefix):
        """
        Restores the model into model_dir from model_prefix as the model indicator
        """
        self.saver.restore(self.sess, os.path.join(model_dir, model_prefix))
        self.logger.info('Model restored from {}, with prefix {}'.format(model_dir, model_prefix))
