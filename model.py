from preprocessing import CharPreprocessor
import preprocessing
import logging
from tqdm import trange
import os
import tensorflow as tf
import numpy as np
import re
import pickle
import data
from tensorflow.contrib.rnn import LSTMStateTuple

MODEL_CHECKPOINT_NAME = "model.ckpt"
MODEL_CHECKPOINT_REGEX = r"^model\.ckpt-(\d+)"

def sample(logits, temperature=0.0):
    # helper function to sample an index from a logit array
    if temperature == 0.0 or temperature is None:
        return np.argmax(logits)

    logits = logits/temperature
    exp_preds = np.exp(logits - np.max(logits))
    preds = exp_preds / np.sum(exp_preds)
    return np.random.choice(range(len(logits)), p=preds)

class FriendChatBot(object):

    def __init__(self, max_vocab_size, unk_token, save_dir, text_col):
        self.preprocessor = CharPreprocessor(text_col=text_col, max_vocab_size=max_vocab_size,
                                             start_end_token=True, unk_token=unk_token)
        self.save_dir = save_dir
        self.is_initialized = False
        self.session = self.get_session()
        self.saver = None

    def get_session(self):
        return tf.Session()

    def build_model(self):
        N_LAYERS = 1
        N_UNITS = 128
        INPUT_KEEP_PROB = 0.8
        OUTPUT_KEEP_PROB = 0.9

        vocab_size = self.preprocessor.shape[1]

        embeddings = tf.get_variable("word_embeddings",
                                     initializer=tf.constant(np.diag(np.ones(vocab_size)), dtype=tf.float32),
                                     trainable=False)

        cells = [tf.contrib.rnn.BasicLSTMCell(num_units=N_UNITS) for _ in range(N_LAYERS)]
        cells = [tf.contrib.rnn.DropoutWrapper(cell, input_keep_prob=INPUT_KEEP_PROB, output_keep_prob=OUTPUT_KEEP_PROB) for cell in cells]
        self.rnn_cell = tf.contrib.rnn.MultiRNNCell(cells)

        # Training
        self.decoder_input_train = tf.placeholder(dtype="int32", shape=(None, None), name="decoder_input")
        decoder_train_embeddings = tf.nn.embedding_lookup(embeddings, self.decoder_input_train)
        self.decoder_input_train_lengths = tf.placeholder(dtype="int32", shape=[None], name="decoder_lengths")
        decoder_outputs_train, _ = tf.nn.dynamic_rnn(self.rnn_cell, decoder_train_embeddings,
                                                     sequence_length=self.decoder_input_train_lengths,
                                                     dtype=tf.float32)
        decoder_proj_train = tf.layers.dense(decoder_outputs_train, units=vocab_size, use_bias=True, name="dense")
        self.decoder_targets = tf.placeholder(dtype="int32", shape=(None, None), name="decoder_targets")

        # Predicting
        self.decoder_input_pred = tf.placeholder(dtype="int32", shape=(None, 1), name="decoder_input_pred")
        decoder_embedding_pred = tf.nn.embedding_lookup(embeddings, self.decoder_input_pred)
        with tf.variable_scope("rnn", reuse=True):
            self.decoders_c = tuple(tf.placeholder(dtype=tf.float32, shape=(None, N_UNITS), name="c{}".format(i)) for i in range(N_LAYERS))
            self.decoders_h = tuple(tf.placeholder(dtype=tf.float32, shape=(None, N_UNITS), name="h{}".format(i)) for i in range(N_LAYERS))

            state = [LSTMStateTuple(self.decoders_c[i], self.decoders_h[i]) for i in range(N_LAYERS)]
            decoder_outputs_pred, self.decoder_states_pred = self.rnn_cell(
                inputs=tf.reshape(decoder_embedding_pred, shape=[-1, vocab_size]),
                state=state)
        self.decoder_proj_pred = tf.layers.dense(decoder_outputs_pred, units=vocab_size, use_bias=True,
                                                 name="dense", reuse=True)

        mask = tf.cast(tf.sequence_mask(self.decoder_input_train_lengths), tf.float32)
        self.loss_op = tf.contrib.seq2seq.sequence_loss(logits=decoder_proj_train,
                                                        targets=self.decoder_targets,
                                                        weights=mask)
        self.train_op = tf.train.AdamOptimizer(1e-4).minimize(self.loss_op)
        self.init_op = tf.global_variables_initializer()

    def fit(self, training_data, num_epochs=9999, batch_size=128):

        def chunker(seq, size):
            while True:
                for pos in range(0, len(seq), size):
                    yield seq[pos:pos + size]

        # Fit preprocessor if not fitted
        if not self.preprocessor.is_fitted:
            self.preprocessor.fit(training_data)
            with open(os.path.join(self.save_dir, "CharPreprocessor.pickle"), "wb") as f:
                pickle.dump(self.preprocessor, f)
            logging.info("Preprocessor fit and saved")

        training_data_gen = chunker(training_data, batch_size)
        steps_per_epoch = len(training_data) // batch_size

        # Model model graph
        if not self.is_initialized:
            self.build_model()
            self.saver = tf.train.Saver()
            self.session.run([self.init_op])
            self.is_initialized = True

        logging.info("Starts training of '{}'".format(type(self).__name__))
        for epoch in range(num_epochs):
            monitor = trange(steps_per_epoch)
            for step in monitor:
                inputs, targets = self.preprocessor.transform(next(training_data_gen))
                self.train_step(inputs, targets, monitor)

            # Save checkpoint
            logging.debug("Saving checkpoint...")
            self.saver.save(self.session, os.path.join(self.save_dir, MODEL_CHECKPOINT_NAME),
                            global_step=epoch * steps_per_epoch + step)
            logging.debug("Checkpoint saved")

    def train_step(self, inputs, targets, tqdm):
        (input_seq, input_lengths)  = inputs
        loss, _ = self.session.run([self.loss_op, self.train_op], feed_dict={
            self.decoder_input_train: input_seq,
            self.decoder_input_train_lengths: input_lengths,
            self.decoder_targets: targets
        })
        tqdm.set_postfix(loss=loss)

    def chat(self, my_name, friend_name, temperature=0.0):

        state_and_hidden = self.session.run([self.rnn_cell.zero_state(1, dtype=tf.float32)])
        state_and_hidden = state_and_hidden[0]

        while True:

            # Me talk
            print(my_name)
            my_message = input()

            for char in data.ME_START_CHAR + my_message:
                if not char in self.preprocessor.vocabulary: continue

                input_char = np.array([[self.preprocessor.vocabulary[char]]])
                state_and_hidden = self.session.run([self.decoder_states_pred],
                                                     feed_dict={
                                                        self.decoder_input_pred: input_char,
                                                        self.decoders_c: [x.c for x in state_and_hidden],
                                                        self.decoders_h: [x.h for x in state_and_hidden],
                                                     })
                state_and_hidden = state_and_hidden[0]

            # Friend talk
            print()
            print(friend_name)

            friend_message = []
            next_input = np.array([[self.preprocessor.vocabulary[data.FRIEND_START_CHAR]]])
            for t in range(150):
                logits, state_and_hidden = self.session.run([self.decoder_proj_pred, self.decoder_states_pred],
                                                            feed_dict={
                                                                self.decoder_input_pred: next_input,
                                                                self.decoders_c: [x.c for x in state_and_hidden],
                                                                self.decoders_h: [x.h for x in state_and_hidden],
                                                            })

                # Temperature == 0 by default, can pick larger for more randomness
                selected = sample(logits[0, :], temperature=temperature)

                if not selected in (self.preprocessor.vocabulary[preprocessing.END_TOKEN],
                                    self.preprocessor.vocabulary[data.ME_START_CHAR]):
                    friend_message.append(self.preprocessor.inv_vocabulary[selected])
                    next_input = np.array([[selected]])
                else:
                    # Predicted end of message char, feed me_start_char and break generation
                    state_and_hidden = self.session.run([self.decoder_states_pred],
                                                        feed_dict={
                                                            self.decoder_input_pred: np.array([[self.preprocessor.vocabulary[data.ME_START_CHAR]]]),
                                                            self.decoders_c: [x.c for x in state_and_hidden],
                                                            self.decoders_h: [x.h for x in state_and_hidden],
                                                        })
                    state_and_hidden = state_and_hidden[0]
                    break
            print("".join(friend_message))
            print()

    def can_load(self):
        regex_results = [re.search(MODEL_CHECKPOINT_REGEX, x) for x in os.listdir(self.save_dir)]
        checkpoints = [m.group(0) for m in regex_results if m]
        return len(checkpoints) > 0

    def load(self):
        with open(os.path.join(self.save_dir, "CharPreprocessor.pickle"), "rb") as f:
            self.preprocessor = pickle.load(f)

        self.build_model()
        self.saver = tf.train.Saver()
        checkpoints = [m for m in map(lambda x: re.search(MODEL_CHECKPOINT_REGEX, x), os.listdir(self.save_dir)) if m]
        checkpoints = sorted(checkpoints, key=lambda x: int(x.group(1)))
        self.saver.restore(self.session, os.path.join(self.save_dir, checkpoints[-1].group(0)))
        self.is_initialized = True
        logging.info("Model loaded successfully from a checkpoint")
