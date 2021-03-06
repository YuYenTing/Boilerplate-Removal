import tensorflow as tf
import numpy as np
import pandas as pd
from glob import glob
from sklearn.metrics import f1_score, precision_recall_fscore_support
import random
from tqdm import tqdm
import os
import math
import argparse
from pathlib import Path
import shutil
from utils import util
import model
from module.dataloader import DataLoader
from loss import Custom_Cross_Entropy

def main(parser):
    args = parser.parse_args()
    MODEL_NAME = "Bayesian" if args.bayesian else "LSTM"
    MODEL_NAME = MODEL_NAME + "-WORD" if args.word else MODEL_NAME  
    MODEL_NAME = MODEL_NAME + "-Emb" if args.tag_rep else MODEL_NAME + "-Vec"
    if args.aux > 0:
        MODEL_NAME = MODEL_NAME + "-Depth-" if args.aux == 1 else MODEL_NAME + "-Pos-"

    # Model Definition
    if args.bayesian:
        myModel = model.MCModel(
            ff_dim=args.hidden_dim,
            num_layers=args.lstm_layer,
            out_dim=args.label_size,
            lr=args.learning_rate,
            lstm_dropout=args.lstm_dropout,
            dropout=args.dropout,
            mc_step=args.mc_step,
            aux=args.aux,
            tag=args.tag_rep,
            emb_init=args.emb_init
        )
    else:
        myModel = model.LSTMModel(
            ff_dim=args.hidden_dim,
            num_layers=args.lstm_layer,
            out_dim=args.label_size,
            lr=args.learning_rate,
            lstm_dropout=args.lstm_dropout,
            dropout=args.dropout,
            mc_step=args.mc_step,
            aux=args.aux,
            tag=args.tag_rep,
            emb_init=args.emb_init
        )

    # Dataset
    myDataLoader = DataLoader(args, myModel)

    # Training
    if args.train:
        print("Start Training.")
        train(args, myDataLoader, myModel)

    BEST_MODEL = ""
    BEST_MODEL = "/best_val/" if args.best_loss_model else BEST_MODEL
    BEST_MODEL = "/best_macro_f1/" if args.best_macro_f1 else BEST_MODEL
    FOLDER = args.checkpoint_folder + MODEL_NAME + str(args.alpha)
    if not os.path.isdir(FOLDER + BEST_MODEL):
        print("Checkpoint doesn't exist. Start training.")
        Path(FOLDER).mkdir(parents=True, exist_ok=True)
        train(args, myDataLoader, myModel)

    # Test
    if BEST_MODEL is "":
        print("Best loss model:")
        test(args,
            myDataLoader,
            myModel,
            checkpoint=FOLDER + "/best_val/",
            total=args.micro)
        print("Best F1 model:")
        test(args,
            myDataLoader,
            myModel,
            checkpoint=FOLDER + "/best_macro_f1/",
            total=args.micro)
    else:
        test(args,
            myDataLoader,
            myModel,
            checkpoint=FOLDER + BEST_MODEL,
            total=args.micro)


def train(args, myDataLoader, myModel):
    MODEL_NAME = "Bayesian" if args.bayesian else "LSTM"
    MODEL_NAME = MODEL_NAME + "-WORD" if args.word else MODEL_NAME
    MODEL_NAME = MODEL_NAME + "-Emb" if args.tag_rep else MODEL_NAME + "-Vec"
    if args.aux > 0:
        MODEL_NAME = MODEL_NAME + "-Depth-" if args.aux == 1 else MODEL_NAME + "-Pos-"
    FOLDER = args.checkpoint_folder + MODEL_NAME + str(args.alpha)
    
    # Compute weight
    print("Calculate class weights...")
    all_y_true = None
    for f in glob(args.train_folder + "*.csv"):
        df = pd.read_csv(f, encoding='utf-8')
        y_true = tf.reshape(np.array(df.label), [-1])
        all_y_true = util.concatAxisZero(all_y_true, y_true)
    unique, counts = np.unique(all_y_true, return_counts=True)
    freq = dict(zip(unique, counts))
    class_weights = tf.constant(
        [[1-freq[l]/len(all_y_true) for l in range(args.label_size)]])
    # New class weight
    class_weights = tf.Variable(class_weights)
    class_weights[0,1].assign(class_weights[0,1]*(freq[0]/freq[1])) # Change content weight to higher
    class_weights = tf.convert_to_tensor(class_weights)
    print("Class weights: ", class_weights)

    # Set up loss
    My_Mask_CE = Custom_Cross_Entropy(class_weights)
    MSE = tf.keras.losses.MeanSquaredError()
    Category_Loss = tf.keras.losses.CategoricalCrossentropy() # Domain classifier loss

    # Set up metrics
    train_loss = tf.keras.metrics.Mean('train_loss', dtype=tf.float32)
    val_loss = tf.keras.metrics.Mean('val_loss', dtype=tf.float32)
    val_macro_f1 = tf.keras.metrics.Mean('val_macro_f1', dtype=tf.float32)
    val_micro_f1 = tf.keras.metrics.Mean('val_micro_f1', dtype=tf.float32)

    # Set up log
    if args.log:
        log_root = args.log_folder + 'gradient_tape/' + \
            MODEL_NAME + "/alpha" + str(args.alpha)
        if os.path.isdir(log_root):
            shutil.rmtree(log_root)
        train_log_dir = log_root + '/train'
        val_macro_log_dir = log_root + '/val_macro'
        val_micro_log_dir = log_root + '/val_micro'
        train_summary_writer = tf.summary.create_file_writer(train_log_dir)
        val_macro_summary_writer = tf.summary.create_file_writer(
            val_macro_log_dir)
        val_micro_summary_writer = tf.summary.create_file_writer(
            val_micro_log_dir)

    # Initialize best loss and f1
    best_loss = float("inf")
    best_macro_f1 = 0

    # Buffer
    if args.no_buffer or args.batch > 1:
        print("Train and val with dataset.")
        train_source = myDataLoader.train_ds
        val_source = myDataLoader.val_ds
    else:
        print("Fill up train and val buffer.")
        train_buffer = []
        val_buffer = []
        train_source = train_buffer
        val_source = val_buffer
        for t, e, y, a, d in myDataLoader.train_ds:
            train_buffer.append([t, e, y, a, d])
        # for t, e, y, a in myDataLoader.train_ds:
        #     train_buffer.append([t, e, y, a])
        for t, e, y in myDataLoader.val_ds:
            val_buffer.append([t, e, y])

    # Start Training
    count = 0
    for epoch in range(args.epoch):
        print("="*10)
        print("Epoch %d/%d" % (epoch+1, args.epoch))
        # =====================================================
        # Training
        # =====================================================
        if not args.no_buffer and args.batch == 1:
            random.shuffle(train_buffer)
        for t, e, y, a, d in train_source:
            redo = True
            while(redo):
                redo = False
                loss = 0
                myModel.mc_step = args.mc_step
                # Train step
                with tf.GradientTape() as tape:
                    count += 1
                    train_out, a_pred, domain_pred = myModel.MC_sampling(    # train_out: predict label, a_pred: predict depth or pos
                        t, e, training=True)
                    loss += (1-args.alpha)*My_Mask_CE(y_true=y, y_pred=train_out) + \
                        args.alpha*MSE(a, a_pred) + Category_Loss(d, domain_pred)
                if args.tag_rep == 0:
                    trainable_variables = myModel.trainable_variables
                else:
                    trainable_variables = myModel.tag_encoder.trainable_variables + myModel.trainable_variables
                
                grads = tape.gradient(loss, trainable_variables)
                clip_grads, _ = tf.clip_by_global_norm(grads, 5.0)

                myModel.Opt.apply_gradients(
                    (grad, var) for (grad, var) in zip(clip_grads, trainable_variables)
                    if grad is not None)
                train_loss(loss)
                if args.log:
                    with train_summary_writer.as_default():
                        tf.summary.scalar('loss', train_loss.result(), step=count)
                if args.verbose:
                    print("train loss:\t%.4f" % train_loss.result())                

                # =====================================================
                # Validation
                # =====================================================
                all_y_true = None
                all_y_pred = None
                macro_f1 = None
                for vt, ve, vy in val_source:
                    # val step
                    val_out, _, __ = myModel.MC_sampling(vt, ve)
                    loss = My_Mask_CE(y_true=vy, y_pred=val_out)
                    val_loss(loss)
                    y_pred = tf.reshape(tf.argmax(val_out, axis=-1), [-1])
                    y_true = tf.reshape(tf.argmax(vy, axis=-1), [-1])

                    all_y_true = util.concatAxisZero(
                        all_y_true, y_true)
                    all_y_pred = util.concatAxisZero(
                        all_y_pred, y_pred)
                    t_f = f1_score(y_true=y_true,
                                y_pred=y_pred,
                                average='macro',
                                labels=[0, 1],
                                zero_division=1)
                    macro_f1 = util.concatAxisZero(
                        macro_f1, np.expand_dims(t_f, 0))

                if args.verbose:
                    print("val loss:\t%.4f" % val_loss.result(), end="")                    
                if val_loss.result() < best_loss:
                    redo = True
                    best_loss = val_loss.result()
                    if not os.path.isdir(FOLDER):
                        Path(FOLDER + "/best_val/").mkdir(parents=True, exist_ok=True)
                        if args.tag_rep == 1:
                            Path(FOLDER + "/best_val/tag_encoder/").mkdir(parents=True, exist_ok=True)
                    myModel.save_weights(FOLDER + "/best_val/")
                    if args.tag_rep == 1:
                        myModel.tag_encoder.save_weights(FOLDER + "/best_val/tag_encoder/")
                    if args.verbose:
                        print("*")
                else:
                    if args.verbose:
                        print("")
                macro_f1 = np.mean(macro_f1)
                micro_f1 = f1_score(
                    y_true=all_y_true, y_pred=all_y_pred, average='macro', zero_division=0)
                val_macro_f1(macro_f1)
                val_micro_f1(micro_f1)
                if args.log:
                    with val_macro_summary_writer.as_default():
                        tf.summary.scalar('f1', val_macro_f1.result(), step=count)
                    with val_micro_summary_writer.as_default():
                        tf.summary.scalar('loss', val_loss.result(), step=count)
                        tf.summary.scalar('f1', val_micro_f1.result(), step=count)
                if args.verbose:
                    print("val_macro_f1:\t%.4f" % macro_f1, end="")
                if macro_f1 > best_macro_f1:
                    redo = True
                    best_macro_f1 = macro_f1
                    if not os.path.isdir(FOLDER):
                        Path(FOLDER + "/best_macro_f1/").mkdir(parents=True, exist_ok=True)
                        if args.tag_rep == 1:
                            Path(FOLDER + "/best_macro_f1/tag_encoder/").mkdir(parents=True, exist_ok=True)
                    myModel.save_weights(
                        FOLDER + "/best_macro_f1/")
                    if args.tag_rep == 1:
                        myModel.tag_encoder.save_weights(
                            FOLDER + "/best_macro_f1/tag_encoder/")
                    if args.verbose:
                        print("*")
                else:
                    if args.verbose:
                        print("")
                if args.verbose:
                    print("val_micro_f1:\t%.4f" % micro_f1, end="")
                    print("")
                    print("-"*5)
                train_loss.reset_states()
                val_loss.reset_states()
                val_macro_f1.reset_states()
                val_micro_f1.reset_states()

def test(args,
         myDataLoader,
         myModel,
         checkpoint="",
         total=False):
    myModel.load_weights(checkpoint)
    if args.tag_rep == 1:
        myModel.tag_encoder.load_weights(checkpoint + "tag_encoder/")
    all_y_true = None
    all_y_pred = None
    f1_history = []
    precision = recall = f1 = None
    for t, e, y in tqdm(myDataLoader.test_ds, total=len(glob(args.test_folder + "*.csv")), desc="Testing..."):
        out, _, __ = myModel.MC_sampling(t, e)
        y_true = tf.reshape(tf.argmax(y, axis=-1), [-1])
        y_pred = tf.reshape(tf.argmax(out, axis=-1), [-1])

        all_y_true = util.concatAxisZero(
            all_y_true, y_true)
        all_y_pred = util.concatAxisZero(
            all_y_pred, y_pred)
        t_p, t_r, t_f, t_s = precision_recall_fscore_support(
            y_true=y_true,
            y_pred=y_pred,
            labels=[0, 1],
            zero_division=1)
        precision = util.concatAxisZero(precision, np.expand_dims(t_p, 0))
        recall = util.concatAxisZero(recall, np.expand_dims(t_r, 0))
        f1 = util.concatAxisZero(f1, np.expand_dims(t_f, 0))
    print("===Micro F1===")
    t_p, t_r, t_f, t_s = precision_recall_fscore_support(
        y_true=all_y_true,
        y_pred=all_y_pred,
        labels=[0, 1],
        zero_division=1)
    print("precision:", t_p)
    print("recall", t_r)
    print("f1", t_f)
    print("macro", np.mean(t_f, axis=0))
    print("===Macro F1===")
    precision = np.mean(precision, axis=0)
    recall = np.mean(recall, axis=0)
    f1 = np.mean(f1, axis=0)
    print("precision:", precision)
    print("recall", recall)
    print("f1", f1)
    print("macro", np.mean(f1, axis=0))


if __name__ == "__main__":
    util.limit_gpu()
    parser = argparse.ArgumentParser()
    parser.add_argument("-bl", "--bayesian", type=int,
                        help="Enable Bayesian LSTM.", default=1)
    parser.add_argument("-b", "--batch", type=int, help="Set batch size.", default=1)
    parser.add_argument("-e", "--epoch", type=int,
                        help="Set your number of epochs.", default=20)
    parser.add_argument("--alpha", type=float,
                        help="Set multitask alpha.", default=0.5)
    parser.add_argument("--aux", type=int,
                        help="Set auxiliary task. (0 for none, 1 for depth, 2 for pos)", default=1)
    parser.add_argument("--tag_rep", type=int,
                        help="Set tag representation. (0 for vector, 1 for embedding)", default=0)
    parser.add_argument("--emb_init", type=int,
                        help="Set tag representation. (0 for random, 1 for cbow, 2 for skip-gram)", default=2)
    parser.add_argument("--dropout", type=float,
                        help="Set model dropout.", default=0.1)
    parser.add_argument("--lstm_dropout", type=float,
                        help="Set model lstm dropout.", default=0.01)
    parser.add_argument("-lr", "--learning_rate", type=float,
                        help="Set model learning rate", default=1e-3)
    parser.add_argument("--lstm_layer", type=int,
                        help="Set number of lstm layers.", default=2)
    parser.add_argument("--hidden_dim", type=int,
                        help="Set hidden dim of model.", default=256)
    parser.add_argument("-mc", "--mc_step", type=int,
                        help="Set mc step of bayesian model.", default=64)
    parser.add_argument("-w", "--word", action="store_true",
                        help="Enable word embedding model.", default=False)
    parser.add_argument("-ls", "--label_size", type=int,
                        help="Set your label size.", default=2)
    parser.add_argument("-l", "--log", action="store_true",
                        help="Enable TensorBoard log.", default=False)
    parser.add_argument("--log_folder", type=str,
                        help="Set log folder.", default="./logs/")
    parser.add_argument("--train_folder", type=str,
                        help="Set train csvs location.", default="./data/cleaneval/train/")
    parser.add_argument("--val_folder", type=str,
                        help="Set val csvs location.", default="./data/cleaneval/val/")
    parser.add_argument("--test_folder", type=str,
                        help="Set test csvs location.", default="./data/cleaneval/test/")
    parser.add_argument("--checkpoint_folder", type=str,
                        help="Set checkpoint folder.", default="./checkpoints/")
    parser.add_argument("--train", action="store_true",
                        help="Train your own model.", default=False)
    parser.add_argument("--best_loss_model", action="store_true",
                        help="Select model using best val loss", default=False)
    parser.add_argument("--best_macro_f1", action="store_true",
                        help="Select model using best macro f1", default=False)
    parser.add_argument("--micro", action="store_true",
                        help="Set evaluation metric as micro f1.", default=False)
    parser.add_argument("--verbose", type=int,
                        help="print out training detail.", default=1)
    parser.add_argument("--no_buffer", action="store_true",
                        help="Turn off buffer for train and val.", default=False)
    main(parser)
