import os
from tqdm import *

from music21 import *
from music21 import converter, instrument, note, chord, stream
import numpy as np
import pandas as pd
import tensorflow as tf
import struct
import base64
import json
import glob
import argparse


# Melody-RNN Format is a sequence of 8-bit integers indicating the following:
# MELODY_NOTE_ON = [0, 127] # (note on at that MIDI pitch)
MELODY_NOTE_OFF = 128  # (stop playing all previous notes)
MELODY_NO_EVENT = 129  # (no change from previous event)
# Each element in the sequence lasts for one sixteenth note.
# This can encode monophonic music only.
MELODY_SIZE = 130

def parse_args():
    parser = argparse.ArgumentParser("Entry script to launch training")
    parser.add_argument("--data-dir", type=str, default = "./data", help="Path to the data directory")
    parser.add_argument("--output-dir", type=str, default = "./outputs", help = "Path to output directory")
    parser.add_argument("--config-path", type=str, required = True, help="Path to the output file")
    parser.add_argument("--checkpoint-path", type = str, default = None,  help="Path to the checkpoint file")
    return parser.parse_args()

def streamToNoteArray(stream):
    """
    Convert a Music21 sequence to a numpy array of int8s into Melody-RNN format:
        0-127 - note on at specified pitch
        128   - note off
        129   - no event
    """
    # Part one, extract from stream
    total_length = np.int(
        np.round(stream.flat.highestTime / 0.25))  # in semiquavers
    stream_list = []
    for element in stream.flat:
        if isinstance(element, note.Note):
            stream_list.append([np.round(
                element.offset / 0.25), np.round(element.quarterLength / 0.25), element.pitch.midi])
        elif isinstance(element, chord.Chord):
            stream_list.append([np.round(element.offset / 0.25), np.round(
                element.quarterLength / 0.25), element.sortAscending().pitches[-1].midi])
    np_stream_list = np.array(stream_list, dtype=np.int)
    df = pd.DataFrame(
        {'pos': np_stream_list.T[0], 'dur': np_stream_list.T[1], 'pitch': np_stream_list.T[2]})
    # sort the dataframe properly
    df = df.sort_values(['pos', 'pitch'], ascending=[True, False])
    df = df.drop_duplicates(subset=['pos'])  # drop duplicate values
    # part 2, convert into a sequence of note events
    # set array full of no events by default.
    output = np.zeros(total_length+1, dtype=np.int16) + \
        np.int16(MELODY_NO_EVENT)
    # Fill in the output list
    for i in range(total_length):
        if not df[df.pos == i].empty:
            # pick the highest pitch at each semiquaver
            n = df[df.pos == i].iloc[0]
            output[i] = n.pitch  # set note on
            output[i+n.dur] = MELODY_NOTE_OFF
    return output

def noteArrayToDataFrame(note_array):
    """
    Convert a numpy array containing a Melody-RNN sequence into a dataframe.
    """
    df = pd.DataFrame({"code": note_array})
    df['offset'] = df.index
    df['duration'] = df.index
    df = df[df.code != MELODY_NO_EVENT]
    # calculate durations and change to quarter note fractions
    df.duration = df.duration.diff(-1) * -1 * 0.25
    df = df.fillna(0.25)
    return df[['code', 'duration']]

def noteArrayToStream(note_array):
    """
    Convert a numpy array containing a Melody-RNN sequence into a music21 stream.
    """
    df = noteArrayToDataFrame(note_array)
    melody_stream = stream.Stream()
    for index, row in df.iterrows():
        if row.code == MELODY_NO_EVENT:
            # bit of an oversimplification, doesn't produce long notes.
            new_note = note.Rest()
        elif row.code == MELODY_NOTE_OFF:
            new_note = note.Rest()
        else:
            new_note = note.Note(row.code)
        new_note.quarterLength = row.duration
        melody_stream.append(new_note)
    return melody_stream

def get_notes_from_file(file_path):
    """ Get all the notes and chords from the midi files in the ./midi_songs directory """
    notes = []

    for file in glob.glob("midi_songs/*.mid"):
        midi = converter.parse(file)

        print("Parsing %s" % file)

        notes_to_parse = None

        try: # file has instrument parts
            s2 = instrument.partitionByInstrument(midi)
            notes_to_parse = s2.parts[0].recurse() 
        except: # file has notes in a flat structure
            notes_to_parse = midi.flat.notes

        for element in notes_to_parse:
            if isinstance(element, note.Note):
                notes.append(str(element.pitch))

    return notes

# making data to train
def make_training_data(data_dir, sequence_length=20):
    notes = []

    fold_paths = glob.glob(os.path.join(data_dir, '*'))
    for fold_path in fold_paths:
        file_paths = glob.glob(os.path.join(fold_path, '*'))
        for file_path in file_paths:
            if file_path.endswith('.mid') or file_path.endswith('.midi'):
                midi = converter.parse(file_path)
                notes_to_parse = None
                try: # file has instrument parts
                    s2 = instrument.partitionByInstrument(midi)
                    notes_to_parse = s2.parts[0].recurse() 
                except: # file has notes in a flat structure
                    notes_to_parse = midi.flat.notes

                for element in notes_to_parse:
                    if isinstance(element, note.Note):
                        notes.append(str(element.pitch))

    pitchnames = sorted(set(item for item in notes))
    # create a dictionary to map pitches to integers
    note_to_int = dict((note, number) for number, note in enumerate(pitchnames))    

    inputs = []
    targets = []
    # create input sequences and the corresponding outputs
    for i in range(0, len(notes) - sequence_length):
        sequence_in = notes[i:i + sequence_length]
        sequence_out = notes[i + sequence_length]
        inputs.append([note_to_int[char] for char in sequence_in])
        targets.append(note_to_int[sequence_out])
    
    n_sequences = len(inputs)
    # reshape the input into a format compatible with LSTM layers
    inputs = np.reshape(inputs, (n_sequences, sequence_length, 1))
    targets = np.array(targets)
    return inputs, targets, note_to_int



def create_model(config, model_path = None):
    rnn_units = config["rnn_units"]
    embedding_dim = config["embedding_dim"]
    n_vocab = config["n_vocab"]
    batch_size = config["batch_size"]

    if model_path is not None:
        model = tf.keras.models.load_model(model_path)
        return model

    model = tf.keras.Sequential()
    model.add(tf.keras.layers.Embedding(
        input_dim= MELODY_SIZE,
        output_dim=embedding_dim,
        batch_input_shape=[batch_size, None]
    ))
    model.add(tf.keras.layers.LSTM(
        units = rnn_units,
        return_sequences=True,
        stateful=True,
    ))

    model.add(tf.keras.layers.LSTM(
        units = rnn_units,
        return_sequences=True,
        stateful=True,
    ))

    model.add(tf.keras.layers.Dense(n_vocab))
    model.add(tf.keras.layers.Activation('softmax'))
    model.compile(loss='categorical_crossentropy', optimizer='rmsprop')
    model.summary()   
    return model


def get_weights(model):
    weights = []
    for layer in model.layers:
        w = layer.get_weights()
        weights.append(w)
    return weights

def compressConfig(data):
    layers = []
    for layer in data["config"]["layers"]:
        cfg = layer["config"]
        if layer["class_name"] == "InputLayer":
            layer_config = {
                "batch_input_shape": cfg["batch_input_shape"]
            }
        elif layer["class_name"] == "Rescaling":
            layer_config = {
                "scale": cfg["scale"],
                "offset": cfg["offset"]
            }
        elif layer["class_name"] == "Dense":
            layer_config = {
                "units": cfg["units"],
                "activation": cfg["activation"]
            }
        elif layer["class_name"] == "Conv2D":
            layer_config = {
                "filters": cfg["filters"],
                "kernel_size": cfg["kernel_size"],
                "strides": cfg["strides"],
                "activation": cfg["activation"],
                "padding": cfg["padding"]
            }
        elif layer["class_name"] == "MaxPooling2D":
            layer_config = {
                "pool_size": cfg["pool_size"],
                "strides": cfg["strides"],
                "padding": cfg["padding"]
            }
        elif layer["class_name"] == "Embedding":
            layer_config = {
                "input_dim": cfg["input_dim"],
                "output_dim": cfg["output_dim"]
            }
        elif layer["class_name"] == "SimpleRNN":
            layer_config = {
                "units": cfg["units"],
                "activation": cfg["activation"]
            }
        elif layer["class_name"] == "LSTM":
            layer_config = {
                "units": cfg["units"],
                "activation": cfg["activation"],
                "recurrent_activation": cfg["recurrent_activation"],
            }
        else:
            layer_config = None

        res_layer = {
            "class_name": layer["class_name"],
        }
        if layer_config is not None:
            res_layer["config"] = layer_config
        layers.append(res_layer)

    return {
        "config": {
            "layers": layers
        }
    }

def get_model_for_export(output_path, model, vocabulary):
    weight_np = get_weights(model)

    weight_bytes = bytearray()
    for idx, layer in enumerate(weight_np):
        # print(layer.shape)
        # write_to_file(f"model_weight_{idx:02}.txt", str(layer))
        for weight_group in layer:
            flatten = weight_group.reshape(-1).tolist()
            # print("flattened length: ", len(flatten))
            for i in flatten:
                weight_bytes.extend(struct.pack("@f", float(i)))

    weight_base64 = base64.b64encode(weight_bytes).decode()
    config = json.loads(model.to_json())
    # print("full config: ", config)

    compressed_config = compressConfig(config)
    # write to file
    with open(output_path, "w") as f:
        json.dump({
            "model_name": "musicnetgen",
            "layers_config": compressed_config,
            "weight_b64": weight_base64,
        }, f)
    return weight_base64, compressed_config

def main():
    args = parse_args()

    data_dir = args.data_dir
    output_dir = args.output_dir
    ckpt = args.checkpoint_path
    config_path = args.config_path
    with open(config_path, 'r') as f:
        config = json.load(f)

    sequence_length = config["seq_length"]
    batch_size = config["batch_size"]
    epochs = config["epoch_num"]

    X, y, note_to_int = make_training_data(data_dir, sequence_length)

    vocabulary = []
    for key, value in note_to_int.items():
        vocabulary.append(key)
    config["n_vocab"] = len(vocabulary)
    model = create_model(config, ckpt)
    checkpoint_callback = tf.keras.callbacks.ModelCheckpoint(
        filepath=os.path.join(output_dir, "model.h5"),
        save_weights_only=True,
        verbose=1
    )
    model.fit(X, y, epochs=epochs, batch_size=batch_size, callbacks=[checkpoint_callback])
    get_model_for_export(os.path.join(output_dir, "model.json"), model)

if __name__ == "__main__":
    main()