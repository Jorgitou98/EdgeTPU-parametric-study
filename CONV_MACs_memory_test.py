from keras.models import Sequential
from tensorflow.keras import layers
from tensorflow.keras import activations
import tensorflow as tf
import numpy as np
import subprocess
import rollout_edge_tpu_basic
import rollout_edge_tpu_pipeline_basic
import rollout_pipeline_batch_CONV
import os
import sys
from contextlib import contextmanager
import re
import csv
import argparse

W = 64
input_shape = (W, W, 3)

parser = argparse.ArgumentParser()
parser.add_argument('--minF', type=int, default=2, help='Minimum number of filters for de experiment')
parser.add_argument('--maxF', type=int, default=2025, help='Maximum number of filters for de experiment')
parser.add_argument('--stepF', type=int, default=100, help='Step number of filters for de experiment')
parser.add_argument('--steps', type=int, default=100, help='Step per inference')
parser.add_argument('--layers', type=int, default=5, help='Number of convolutional layers.')
parser.add_argument('--segments-list', nargs='+', type=int, default=1, help='List with number of segments for test.')
parser.add_argument('--batch-size', type=int, default=1, help='Batch size for execution execution.')
parser.add_argument('--profile-partition', type=bool, default=False, help='Flag for pipeline segmentation using profiling')
parser.add_argument('--profiling-diff-threshold', type=int, default=500000, help='Threshold between the slowest and fastest segment in ns')
args = parser.parse_args()

minF = args.minF
maxF = args.maxF
stepF = args.stepF
steps = args.steps
L = args.layers
segments_list = args.segments_list
batch_size = args.batch_size
profile_partition = args.profile_partition
profiling_diff_threshold = args.profiling_diff_threshold

def convert_to_TFLite(model_file_prefix, keras_model):
    converter = tf.lite.TFLiteConverter.from_keras_model(keras_model)
    tflite_model = converter.convert()
    open(model_file_prefix + ".tflite", "wb").write(tflite_model)
    return tflite_model

def representative_data_gen():
    for input_value in np.array(np.random.random_sample([100] + list(input_shape)), dtype=np.float32):
        yield [np.array([input_value])]

def quantize(model_file_prefix, keras_model):
    converter = tf.lite.TFLiteConverter.from_keras_model(keras_model)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]

    converter.representative_dataset = representative_data_gen

    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    #converter.inference_input_type = tf.int8
    converter.inference_output_type = tf.int8

    tflite_model_quant = converter.convert()
    open(model_file_prefix + "_quant.tflite", "wb").write(tflite_model_quant)

def memory_use(line_init, num_segments):
  f = open(f"CONV_MACs/compile_info/compiler_aux", 'r')
  lines_mem_used = [line for line in f.readlines() if line_init in line]
  f.close()
  memory_uses= []
  for line_mem_used in lines_mem_used:
    data_parsed = re.search(f"{line_init} (.+?)(B|KiB|MiB)", line_mem_used)
    print(data_parsed)
    #input("continuar")
    mem = data_parsed.group(1)
    mem_magnitude = data_parsed.group(2)
    print(f"{line_init}: {mem}{mem_magnitude}")
    mem_MB = float(mem)
    if mem_magnitude == "KiB":
      mem_MB /= 1024
    elif mem_magnitude == "B":
      mem_MB /= (1024 * 1024)
    memory_uses.append(mem_MB)
  print(memory_uses)
  #input("continuar")
  return memory_uses

def test_edge_tpu(model_file_prefix, num_segments):
    orig_stdout = os.dup(sys.stdout.fileno())
    f = open(f"CONV_MACs/compile_info/compiler_aux", 'w')
    os.dup2(f.fileno(), sys.stdout.fileno())
    edge_tpu = f'edgetpu_compiler --num_segments {num_segments} -o CONV_MACs/ {model_file_prefix}_quant.tflite'
    if profile_partition:
      edge_tpu = f'./libcoral/out/k8/tools/partitioner/partition_with_profiling --edgetpu_compiler_binary /usr/bin/edgetpu_compiler --model_path {model_file_prefix}_quant.tflite --num_segments {num_segments} --diff_threshold_ns {profiling_diff_threshold} --output_dir CONV_MACs/'
    subprocess.Popen(edge_tpu.split()).communicate()
    os.dup2(orig_stdout, sys.stdout.fileno())
    f.close()
    if num_segments > 1 and not os.path.exists(f"{model_file_prefix}_quant_segment_0_of_{num_segments}_edgetpu.tflite"):
      return (None, None, None)

    on_chip_mems_MB = memory_use(line_init="On-chip memory used for caching model parameters:", num_segments=num_segments)[-num_segments:]
    off_chip_mems_MB = memory_use(line_init="Off-chip memory used for streaming uncached model parameters:", num_segments=num_segments)[-num_segments:]
    #if num_segments == 1:
      #inf_time = rollout_edge_tpu_basic.execute(model_path=f"{model_file_prefix}_quant_edgetpu.tflite", steps = steps)
    #else:
      #inf_time = rollout_edge_tpu_pipeline_basic.execute(model_prefix=f"{model_file_prefix}_quant", num_segments = num_segments, steps = steps)
    inf_time = rollout_pipeline_batch_CONV.execute(model_prefix=f"{model_file_prefix}_quant", num_segments = num_segments, steps = steps, batch_size = batch_size)
    print(f"Inference time {num_segments} segments:", inf_time)
    return (inf_time, on_chip_mems_MB, off_chip_mems_MB)


for num_segments in segments_list:
  csv_results = open(f"CONV_MACs/results/minF{minF}-maxF{maxF}-stepF{stepF}-seg{num_segments}-batch{batch_size}-profiling{profile_partition}.csv", "w")
  writer_results = csv.writer(csv_results, delimiter=',')
  writer_results.writerow(["Num filters", "# MACs", "On chip mem used", "Off chip mem used", "Inference time"])
  csv_results.close()

for num_filters in range(minF, maxF+1, stepF):
    # w^2 * 3*2 es aplicar un filtro sobre un canal. Hay que hacerlo tantas veces como canales de entrada para cada canal de salida. El +1 es por los bias
    num_MACs = W * W * 3 * 3 * ((3+1)*num_filters + (num_filters+1)*num_filters*(L-1))
    print("num_MACs:", num_MACs, "num_filters:", num_filters, "input_shape:", input_shape)
    model = Sequential()
    model.add(layers.Conv2D(num_filters, (3,3), padding='same', input_shape=input_shape, activation='relu'))
    for _ in range(L-1):
      model.add(layers.Conv2D(num_filters, (3,3), padding='same', activation='relu'))
    print(model.summary())
    #input("continue")

    model_file_prefix = f"CONV_MACs/N{num_filters}-nMACs{num_MACs}"
    tflite_model = convert_to_TFLite(model_file_prefix = model_file_prefix, keras_model = model)
    interpreter = tf.lite.Interpreter(model_content=tflite_model)
    interpreter.allocate_tensors()  # Needed before execution!
    output_details = interpreter.get_output_details()[0]  # Model has single output.
    input_details = interpreter.get_input_details()[0]  # Model has single input.
    print("Output:", output_details)
    print("Input:", input_details)

    quantize(model_file_prefix, model)

    for num_segments in segments_list:
      inf_time, on_chip_mem_MB, off_chip_mem_MB = test_edge_tpu(model_file_prefix=model_file_prefix, num_segments=num_segments)
      print(inf_time, on_chip_mem_MB, off_chip_mem_MB)
      #input("continuar")
      csv_results = open(f"CONV_MACs/results/minF{minF}-maxF{maxF}-stepF{stepF}-seg{num_segments}-batch{batch_size}-profiling{profile_partition}.csv", "a")
      writer_results = csv.writer(csv_results, delimiter=',')
      writer_results.writerow([num_filters, num_MACs, on_chip_mem_MB, off_chip_mem_MB, inf_time])
csv_results.close()
