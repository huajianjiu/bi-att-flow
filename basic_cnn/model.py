import random

import itertools
import numpy as np
import tensorflow as tf
from tensorflow.python.ops.rnn_cell import BasicLSTMCell, GRUCell

from basic_cnn.read_data import DataSet
from basic_cnn.superhighway import SHCell
from my.tensorflow import exp_mask, get_initializer, VERY_SMALL_NUMBER
from my.tensorflow.nn import linear, double_linear_logits, linear_logits, softsel, dropout, get_logits, softmax, \
    highway_network, multi_conv1d
from my.tensorflow.rnn import bidirectional_dynamic_rnn, dynamic_rnn
from my.tensorflow.rnn_cell import SwitchableDropoutWrapper, AttentionCell


def bi_attention(config, is_train, h, u, h_mask=None, u_mask=None, scope=None, tensor_dict=None):
    """
    h_a:
    all u attending on h
    choosing an element of h that max-matches u
    First creates confusion matrix between h and u
    Then take max of the attention weights over u row
    Finally softmax over

    u_a:
    each h attending on u

    :param h: [N, M, JX, d]
    :param u: [N, JQ, d]
    :param h_mask:  [N, M, JX]
    :param u_mask:  [N, B]
    :param scope:
    :return: [N, M, d], [N, M, JX, d]
    """
    with tf.variable_scope(scope or "bi_attention"):
        N, M, JX, JQ, d = config.batch_size, config.max_num_sents, config.max_sent_size, config.max_ques_size, config.hidden_size
        JX = tf.shape(h)[2]
        JQ = tf.shape(u)[1]
        h_aug = tf.tile(tf.expand_dims(h, 3), [1, 1, 1, JQ, 1])
        u_aug = tf.tile(tf.expand_dims(tf.expand_dims(u, 1), 1), [1, M, JX, 1, 1])
        if h_mask is None:
            and_mask = None
        else:
            h_mask_aug = tf.tile(tf.expand_dims(h_mask, 3), [1, 1, 1, JQ])
            u_mask_aug = tf.tile(tf.expand_dims(tf.expand_dims(u_mask, 1), 1), [1, M, JX, 1])
            and_mask = h_mask_aug & u_mask_aug

        u_logits = get_logits([h_aug, u_aug], None, True, wd=config.wd, mask=and_mask,
                              is_train=is_train, func=config.logit_func, scope='u_logits')  # [N, M, JX, JQ]
        u_a = softsel(u_aug, u_logits)  # [N, M, JX, d]
        if tensor_dict is not None:
            # a_h = tf.nn.softmax(h_logits)  # [N, M, JX]
            a_u = tf.nn.softmax(u_logits)  # [N, M, JX, JQ]
            # tensor_dict['a_h'] = a_h
            tensor_dict['a_u'] = a_u
        if config.bi:
            h_a = softsel(h, tf.reduce_max(u_logits, 3))  # [N, M, d]
            h_a = tf.tile(tf.expand_dims(h_a, 2), [1, 1, JX, 1])
        else:
            h_a = None
        return u_a, h_a


def attention_layer(config, is_train, h, u, h_mask=None, u_mask=None, scope=None, tensor_dict=None):
    with tf.variable_scope(scope or "attention_layer"):
        u_a, h_a = bi_attention(config, is_train, h, u, h_mask=h_mask, u_mask=u_mask, tensor_dict=tensor_dict)
        if config.bi:
            p0 = tf.concat(3, [h , u_a, h * u_a, h * h_a])
        else:
            p0 = tf.concat(3, [h , u_a, h * u_a])
        return p0


class Model(object):
    def __init__(self, config, scope):
        self.scope = scope
        self.config = config
        self.global_step = tf.get_variable('global_step', shape=[], dtype='int32',
                                           initializer=tf.constant_initializer(0), trainable=False)

        # Define forward inputs here
        N, M, JX, JQ, VW, VC, W = \
            config.batch_size, config.max_num_sents, config.max_sent_size, \
            config.max_ques_size, config.word_vocab_size, config.char_vocab_size, config.max_word_size
        self.x = tf.placeholder('int32', [N, M, None], name='x')
        self.cx = tf.placeholder('int32', [N, M, None, W], name='cx')
        self.x_mask = tf.placeholder('bool', [N, M, None], name='x_mask')
        self.q = tf.placeholder('int32', [N, None], name='q')
        self.cq = tf.placeholder('int32', [N, None, W], name='cq')
        self.q_mask = tf.placeholder('bool', [N, None], name='q_mask')
        self.e_mask = tf.placeholder('bool', [N, M, None], name='e_mask')
        self.y = tf.placeholder('bool', [N, M, None], name='y')
        self.is_train = tf.placeholder('bool', [], name='is_train')
        self.new_emb_mat = tf.placeholder('float', [None, config.word_emb_size], name='new_emb_mat')

        # Define misc
        self.tensor_dict = {}

        # Forward outputs / loss inputs
        self.logits = None
        self.yp = None
        self.var_list = None

        # Loss outputs
        self.loss = None

        self._build_forward()
        self._build_loss()
        if config.mode == 'train':
            self._build_ema()

        self.summary = tf.merge_all_summaries()
        self.summary = tf.merge_summary(tf.get_collection("summaries", scope=self.scope))

    def _build_forward(self):
        config = self.config
        N, M, JX, JQ, VW, VC, d, W = \
            config.batch_size, config.max_num_sents, config.max_sent_size, \
            config.max_ques_size, config.word_vocab_size, config.char_vocab_size, config.hidden_size, \
            config.max_word_size
        JX = tf.shape(self.x)[2]
        JQ = tf.shape(self.q)[1]
        dc, dw, dco = config.char_emb_size, config.word_emb_size, config.char_out_size

        with tf.variable_scope("emb"):
            with tf.variable_scope("emb_var"), tf.device("/cpu:0"):
                char_emb_mat = tf.get_variable("char_emb_mat", shape=[VC, dc], dtype='float')

            with tf.variable_scope("char"):
                Acx = tf.nn.embedding_lookup(char_emb_mat, self.cx)  # [N, M, JX, W, dc]
                Acq = tf.nn.embedding_lookup(char_emb_mat, self.cq)  # [N, JQ, W, dc]
                Acx = tf.reshape(Acx, [-1, JX, W, dc])
                Acq = tf.reshape(Acq, [-1, JQ, W, dc])

                filter_sizes = list(map(int, config.out_channel_dims.split(',')))
                heights = list(map(int, config.filter_heights.split(',')))
                assert sum(filter_sizes) == dco
                with tf.variable_scope("conv"):
                    xx = multi_conv1d(Acx, filter_sizes, heights, "VALID",  self.is_train, config.keep_prob, scope="xx")
                    if config.share_cnn_weights:
                        tf.get_variable_scope().reuse_variables()
                        qq = multi_conv1d(Acq, filter_sizes, heights, "VALID", self.is_train, config.keep_prob, scope="xx")
                    else:
                        qq = multi_conv1d(Acq, filter_sizes, heights, "VALID", self.is_train, config.keep_prob, scope="qq")
                    xx = tf.reshape(xx, [-1, M, JX, dco])
                    qq = tf.reshape(qq, [-1, JQ, dco])

            if config.use_word_emb:
                with tf.variable_scope("emb_var"), tf.device("/cpu:0"):
                    if config.mode == 'train':
                        word_emb_mat = tf.get_variable("word_emb_mat", dtype='float', shape=[VW, dw], initializer=get_initializer(config.emb_mat))
                    else:
                        word_emb_mat = tf.get_variable("word_emb_mat", shape=[VW, dw], dtype='float')
                    if config.use_glove_for_unk:
                        word_emb_mat = tf.concat(0, [word_emb_mat, self.new_emb_mat])

                with tf.name_scope("word"):
                    Ax = tf.nn.embedding_lookup(word_emb_mat, self.x)  # [N, M, JX, d]
                    Aq = tf.nn.embedding_lookup(word_emb_mat, self.q)  # [N, JQ, d]
                    self.tensor_dict['x'] = Ax
                    self.tensor_dict['q'] = Aq
                xx = tf.concat(3, [xx, Ax])  # [N, M, JX, di]
                qq = tf.concat(2, [qq, Aq])  # [N, JQ, di]

        # highway network
        with tf.variable_scope("highway"):
            xx = highway_network(xx, config.highway_num_layers, True, wd=config.wd, is_train=self.is_train)
            tf.get_variable_scope().reuse_variables()
            qq = highway_network(qq, config.highway_num_layers, True, wd=config.wd, is_train=self.is_train)
            self.tensor_dict['xx'] = xx
            self.tensor_dict['qq'] = qq

        cell = BasicLSTMCell(d, state_is_tuple=True)
        d_cell = SwitchableDropoutWrapper(cell, self.is_train, input_keep_prob=config.input_keep_prob)
        x_len = tf.reduce_sum(tf.cast(self.x_mask, 'int32'), 2)  # [N, M]
        q_len = tf.reduce_sum(tf.cast(self.q_mask, 'int32'), 1)  # [N]

        with tf.variable_scope("prepro"):
            (fw_u, bw_u), ((_, fw_u_f), (_, bw_u_f)) = bidirectional_dynamic_rnn(d_cell, d_cell, qq, q_len, dtype='float', scope='u1')  # [N, J, d], [N, d]
            u = tf.concat(2, [fw_u, bw_u])
            if config.two_prepro_layers:
                (fw_u, bw_u), ((_, fw_u_f), (_, bw_u_f)) = bidirectional_dynamic_rnn(d_cell, d_cell, u, q_len, dtype='float', scope='u2')  # [N, J, d], [N, d]
                u = tf.concat(2, [fw_u, bw_u])
            if config.share_lstm_weights:
                tf.get_variable_scope().reuse_variables()
                (fw_h, bw_h), _ = bidirectional_dynamic_rnn(cell, cell, xx, x_len, dtype='float', scope='u1')  # [N, M, JX, 2d]
                h = tf.concat(3, [fw_h, bw_h])  # [N, M, JX, 2d]
                if config.two_prepro_layers:
                    (fw_h, bw_h), _ = bidirectional_dynamic_rnn(cell, cell, h, x_len, dtype='float', scope='u2')  # [N, M, JX, 2d]
                    h = tf.concat(3, [fw_h, bw_h])  # [N, M, JX, 2d]

            else:
                (fw_h, bw_h), _ = bidirectional_dynamic_rnn(cell, cell, xx, x_len, dtype='float', scope='h1')  # [N, M, JX, 2d]
                h = tf.concat(3, [fw_h, bw_h])  # [N, M, JX, 2d]
                if config.two_prepro_layers:
                    (fw_h, bw_h), _ = bidirectional_dynamic_rnn(cell, cell, h, x_len, dtype='float', scope='h2')  # [N, M, JX, 2d]
                    h = tf.concat(3, [fw_h, bw_h])  # [N, M, JX, 2d]
            self.tensor_dict['u'] = u
            self.tensor_dict['h'] = h

        with tf.variable_scope("main"):
            p0 = attention_layer(config, self.is_train, h, u, h_mask=self.x_mask, u_mask=self.q_mask, scope="p0", tensor_dict=self.tensor_dict)
            (fw_g0, bw_g0), _ = bidirectional_dynamic_rnn(d_cell, d_cell, p0, x_len, dtype='float', scope='g0')  # [N, M, JX, 2d]
            g0 = tf.concat(3, [fw_g0, bw_g0])
            # p1 = attention_layer(config, self.is_train, g0, u, h_mask=self.x_mask, u_mask=self.q_mask, scope="p1")
            (fw_g1, bw_g1), _ = bidirectional_dynamic_rnn(d_cell, d_cell, g0, x_len, dtype='float', scope='g1')  # [N, M, JX, 2d]
            g1 = tf.concat(3, [fw_g1, bw_g1])
            # logits = u_logits(config, self.is_train, g1, u, h_mask=self.x_mask, u_mask=self.q_mask, scope="logits")
            # [N, M, JX]
            logits = get_logits([g1, p0], d, True, wd=config.wd, input_keep_prob=config.input_keep_prob, mask=self.e_mask, is_train=self.is_train, func=config.answer_func, scope='logits1')

            flat_logits = tf.reshape(logits, [-1, M * JX])
            flat_yp = tf.nn.softmax(flat_logits)  # [-1, M*JX]
            yp = tf.reshape(flat_yp, [-1, M, JX])

            self.tensor_dict['g1'] = g1

            self.logits = flat_logits
            self.yp = yp

    def _build_loss(self):
        config = self.config
        N, M, JX, JQ, VW, VC = \
            config.batch_size, config.max_num_sents, config.max_sent_size, \
            config.max_ques_size, config.word_vocab_size, config.char_vocab_size
        JX = tf.shape(self.x)[2]
        loss_mask = tf.reduce_max(tf.cast(self.q_mask, 'float'), 1)
        if config.max_answer:
            losses = -tf.log(tf.reduce_max(self.yp * tf.cast(self.y, 'float'), [1, 2]) + VERY_SMALL_NUMBER)
        else:
            losses = -tf.log(tf.reduce_sum(self.yp * tf.cast(self.y, 'float'), [1, 2]) + VERY_SMALL_NUMBER)
        ce_loss = tf.reduce_mean(loss_mask * losses)
        tf.add_to_collection('losses', ce_loss)

        self.loss = tf.add_n(tf.get_collection('losses', scope=self.scope), name='loss')
        tf.scalar_summary(self.loss.op.name, self.loss)
        tf.add_to_collection('ema/scalar', self.loss)

    def _build_ema(self):
        ema = tf.train.ExponentialMovingAverage(self.config.decay)
        ema_op = ema.apply(tf.get_collection("ema/scalar", scope=self.scope) + tf.get_collection("ema/histogram", scope=self.scope))
        for var in tf.get_collection("ema/scalar", scope=self.scope):
            ema_var = ema.average(var)
            tf.scalar_summary(ema_var.op.name, ema_var)
        for var in tf.get_collection("ema/histogram", scope=self.scope):
            ema_var = ema.average(var)
            tf.histogram_summary(ema_var.op.name, ema_var)

        with tf.control_dependencies([ema_op]):
            self.loss = tf.identity(self.loss)

    def get_loss(self):
        return self.loss

    def get_global_step(self):
        return self.global_step

    def get_var_list(self):
        return self.var_list

    def get_feed_dict(self, batch, is_train, supervised=True):
        assert isinstance(batch, DataSet)
        config = self.config
        N, M, JX, JQ, VW, VC, d, W = \
            config.batch_size, config.max_num_sents, config.max_sent_size, \
            config.max_ques_size, config.word_vocab_size, config.char_vocab_size, config.hidden_size, config.max_word_size
        feed_dict = {}

        if config.len_opt:
            """
            Note that this optimization results in variable GPU RAM usage (i.e. can cause OOM in the middle of training.)
            First test without len_opt and make sure no OOM, and use len_opt
            """
            if sum(len(sent) for para in batch.data['x'] for sent in para) == 0:
                new_JX = 1
            else:
                new_JX = max(len(sent) for para in batch.data['x'] for sent in para)
            JX = min(JX, new_JX)

            if sum(len(ques) for ques in batch.data['q']) == 0:
                new_JQ = 1
            else:
                new_JQ = max(len(ques) for ques in batch.data['q'])
            JQ = min(JQ, new_JQ)
        # print(JX)

        x = np.zeros([N, M, JX], dtype='int32')
        cx = np.zeros([N, M, JX, W], dtype='int32')
        x_mask = np.zeros([N, M, JX], dtype='bool')
        e_mask = np.zeros([N, M, JX], dtype='bool')
        q = np.zeros([N, JQ], dtype='int32')
        cq = np.zeros([N, JQ, W], dtype='int32')
        q_mask = np.zeros([N, JQ], dtype='bool')

        feed_dict[self.x] = x
        feed_dict[self.x_mask] = x_mask
        feed_dict[self.cx] = cx
        feed_dict[self.q] = q
        feed_dict[self.cq] = cq
        feed_dict[self.q_mask] = q_mask
        feed_dict[self.e_mask] = e_mask
        feed_dict[self.is_train] = is_train
        if config.use_glove_for_unk:
            feed_dict[self.new_emb_mat] = batch.shared['new_emb_mat']

        X = batch.data['x']
        CX = batch.data['cx']

        def _get_word(word):
            d = batch.shared['word2idx']
            for each in (word, word.lower(), word.capitalize(), word.upper()):
                if each in d:
                    return d[each]
            if config.use_glove_for_unk:
                d2 = batch.shared['new_word2idx']
                for each in (word, word.lower(), word.capitalize(), word.upper()):
                    if each in d2:
                        return d2[each] + len(d)
            return 1

        def _get_char(char):
            d = batch.shared['char2idx']
            if char in d:
                return d[char]
            return 1

        if supervised:
            y = np.zeros([N, M, JX], dtype='int32')
            feed_dict[self.y] = y

            for i, (xi, yi) in enumerate(zip(batch.data['x'], batch.data['y'])):
                count = 0
                for j, xij in enumerate(xi):
                    if j == config.max_num_sents:
                        break
                    for k, xijk in enumerate(xij):
                        if k == JX:
                            break
                        if xijk == yi:
                            y[i, j, k] = True
                            count += 1
                        if xijk.startswith("@"):
                            e_mask[i, j, k] = True
                assert count > 0

        for i, xi in enumerate(X):
            for j, xij in enumerate(xi):
                if j == config.max_num_sents:
                    break
                for k, xijk in enumerate(xij):
                    if k == JX:
                        break
                    each = _get_word(xijk)
                    x[i, j, k] = each
                    x_mask[i, j, k] = True

        for i, cxi in enumerate(CX):
            for j, cxij in enumerate(cxi):
                if j == config.max_num_sents:
                    break
                for k, cxijk in enumerate(cxij):
                    if k == JX:
                        break
                    if _get_word(X[i][j][k]) == 2:
                        cx[i, j, k, 0] = 2
                        continue
                    elif _get_word(X[i][j][k]) == 3:
                        cx[i, j, k, 0] = 3
                        continue
                    for l, cxijkl in enumerate(cxijk):
                        if l == config.max_word_size:
                            break
                        cx[i, j, k, l] = _get_char(cxijkl)

        for i, qi in enumerate(batch.data['q']):
            for j, qij in enumerate(qi):
                if j == JQ:
                    break
                q[i, j] = _get_word(qij)
                q_mask[i, j] = True

        for i, cqi in enumerate(batch.data['cq']):
            for j, cqij in enumerate(cqi):
                if j == JQ:
                    break
                for k, cqijk in enumerate(cqij):
                    if k == config.max_word_size:
                        break
                    cq[i, j, k] = _get_char(cqijk)

        return feed_dict


def get_multi_gpu_models(config):
    models = []
    for gpu_idx in range(config.num_gpus):
        with tf.name_scope("model_{}".format(gpu_idx)) as scope, tf.device("/gpu:{}".format(gpu_idx)):
            model = Model(config, scope)
            tf.get_variable_scope().reuse_variables()
            models.append(model)
    return models
