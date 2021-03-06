import argparse
import logging
import os
import tensorflow as tf
from distutils.util import strtobool

from generic.data_provider.iterator import Iterator
from generic.tf_utils.evaluator import Evaluator, MultiGPUEvaluator
from generic.tf_utils.optimizer import create_optimizer,  create_multi_gpu_optimizer
from generic.tf_utils.ckpt_loader import load_checkpoint, create_resnet_saver
from generic.utils.config import load_config
from generic.utils.file_handlers import pickle_dump
from generic.utils.thread_pool import create_cpu_pool
from generic.data_provider.image_loader import get_img_builder

from clevr.data_provider.clevr_tokenizer import CLEVRTokenizer
from clevr.data_provider.clevr_dataset import CLEVRDataset
from clevr.data_provider.clevr_batchifier import CLEVRBatchifier
from clevr.models.clever_network_factory import create_network


###############################
#  LOAD CONFIG
#############################

parser = argparse.ArgumentParser('CLEVR network baseline!')

parser.add_argument("-data_dir", type=str, help="Directory with data")
parser.add_argument("-img_dir", type=str, help="Directory with image")
parser.add_argument("-img_buf", type=lambda x:bool(strtobool(x)), default="False", help="Store image in memory (faster but require a lot of RAM)")
parser.add_argument("-exp_dir", type=str, help="Directory in which experiments are stored")
parser.add_argument("-config", type=str, help='Config file')
parser.add_argument("-load_checkpoint", type=str, help="Load model parameters from specified checkpoint")
parser.add_argument("-continue_exp", type=lambda x:bool(strtobool(x)), default="False", help="Continue previously started experiment?")
parser.add_argument("-no_thread", type=int, default=1, help="No thread to load batch")
parser.add_argument("-no_gpu", type=int, default=1, help="How many gpus?")
parser.add_argument("-gpu_ratio", type=float, default=0.95, help="How many GPU ram is required? (ratio)")


args = parser.parse_args()

config, exp_identifier, save_path = load_config(args.config, args.exp_dir)
logger = logging.getLogger()


# Load config
resnet_version = config['model']["image"].get('resnet_version', 50)
finetune = config["model"]["image"].get('finetune', list())
lrt = config['optimizer']['learning_rate']
batch_size = config['optimizer']['batch_size']
clip_val = config['optimizer']['clip_val']
no_epoch = config["optimizer"]["no_epoch"]
merge_dataset = config.get("merge_dataset", False)


# Load images
logger.info('Loading images..')
image_builder = get_img_builder(config['model']['image'], args.img_dir, bufferize=args.img_buf)
use_resnet = image_builder.is_raw_image()


# Load dictionary
logger.info('Loading dictionary..')
tokenizer = CLEVRTokenizer(os.path.join(args.data_dir, config["dico_name"]))

# Load data
logger.info('Loading data..')
trainset = CLEVRDataset(args.data_dir, which_set="train", image_builder=image_builder)
validset = CLEVRDataset(args.data_dir, which_set="val", image_builder=image_builder)
testset = CLEVRDataset(args.data_dir, which_set="test", image_builder=image_builder)



# Build Network
logger.info('Building multi_gpu network..')
networks = []
tower_scope_names = []
for i in range(args.no_gpu):
    logging.info('Building network ({})'.format(i))

    with tf.device('gpu:{}'.format(i)):
        with tf.name_scope('tower_{}'.format(i)) as tower_scope:

            network = create_network(
                config=config["model"],
                no_words=tokenizer.no_words,
                no_answers=tokenizer.no_answers,
                reuse=(i > 0), device=i)

            networks.append(network)
            tower_scope_names.append(os.path.join(tower_scope, network.scope_name))


assert len(networks) > 0, "you need to set no_gpu > 0 even if you are using CPU"




# Build Optimizer
logger.info('Building optimizer..')
#optimize, outputs = create_optimizer(networks[0], config["optimizer"], finetune=finetune)
optimize, outputs = create_multi_gpu_optimizer(networks, config["optimizer"], finetune=finetune)

###############################
#  START  TRAINING
#############################

# create a saver to store/load checkpoint
saver = tf.train.Saver()
resnet_saver = None

# Retrieve only resnet variables
if use_resnet:
    resnet_saver = create_resnet_saver(networks)



gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=args.gpu_ratio)

with tf.Session(config=tf.ConfigProto(gpu_options=gpu_options, allow_soft_placement=True)) as sess:

    # retrieve incoming sources
    sources = networks[0].get_sources(sess)
    logger.info("Sources: " + ', '.join(sources))

    # Load checkpoints or pre-trained networks
    sess.run(tf.global_variables_initializer())
    start_epoch = load_checkpoint(sess, saver, args, save_path)
    if use_resnet:
        resnet_saver.restore(sess, os.path.join(args.data_dir,'resnet_v1_{}.ckpt'.format(resnet_version)))

    # Create evaluation tools
    evaluator = MultiGPUEvaluator(sources, tower_scope_names, networks=networks, tokenizer=tokenizer)
    #evaluator = Evaluator(sources, scope=networks[0].scope_name, network=networks[0], tokenizer=tokenizer)
    train_batchifier = CLEVRBatchifier(tokenizer, sources)
    eval_batchifier = CLEVRBatchifier(tokenizer, sources)

    # start actual training
    best_val_acc, best_train_acc = 0, 0
    for t in range(start_epoch, no_epoch):

        # CPU
        cpu_pool = create_cpu_pool(args.no_thread, use_process=image_builder.require_multiprocess())

        logger.info('Epoch {}/{}..'.format(t + 1,no_epoch))

        train_iterator = Iterator(trainset,
                                  batch_size=batch_size,
                                  batchifier=train_batchifier,
                                  shuffle=True,
                                  pool=cpu_pool)
        [train_loss, train_accuracy] = evaluator.process(sess, train_iterator, outputs=outputs + [optimize])


        valid_loss, valid_accuracy = 0,0
        if not merge_dataset:
            valid_iterator = Iterator(validset,
                                      batch_size=batch_size*2,
                                      batchifier=eval_batchifier,
                                      shuffle=False,
                                      pool=cpu_pool)

            [valid_loss, valid_accuracy] = evaluator.process(sess, valid_iterator, outputs=outputs)

        logger.info("Training loss: {}".format(train_loss))
        logger.info("Training accuracy: {}".format(train_accuracy))
        logger.info("Validation loss: {}".format(valid_loss))
        logger.info("Validation accuracy: {}".format(valid_accuracy))

        if valid_accuracy >= best_val_acc:
            best_train_acc = train_accuracy
            best_val_acc = valid_accuracy
            saver.save(sess, save_path.format('params.ckpt'))
            logger.info("checkpoint saved...")

            pickle_dump({'epoch': t}, save_path.format('status.pkl'))

