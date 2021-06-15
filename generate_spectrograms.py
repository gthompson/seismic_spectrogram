import configparser
import csv
import os

from concurrent.futures import ProcessPoolExecutor
from io import StringIO

import numpy
import pandas
import requests


from matplotlib import pyplot as plt
from matplotlib import dates as mdates
from matplotlib.colors import Normalize
from obspy.clients.earthworm import Client as WClient
from obspy import UTCDateTime
from scipy.signal import spectrogram

from GenerateColormap import generate_colormap


def init_generation(config):
    global CONFIG

    CONFIG = config


def main():
    config = configparser.ConfigParser()
    config.read("config.ini")

    # TODO: figure out and loop through all time ranges that need to be generated
    # (i.e. current, missed in previous run, etc)

    # Set endtime to the closest 10 minute mark prior to current time
    ENDTIME = UTCDateTime()
    ENDTIME = ENDTIME.replace(minute=ENDTIME.minute - (ENDTIME.minute % 10),
                              second=0,
                              microsecond=0)

    STARTTIME = ENDTIME - (config['GLOBAL'].getint('minutesperimage', 10) * 60)

#     # DEBUG: Force a specific date/time range
#     ENDTIME = UTCDateTime(2021, 6, 3, 14, 50)
#     STARTTIME = UTCDateTime(2021, 6, 3, 14, 40)

    year = str(ENDTIME.year)
    month = str(ENDTIME.month)
    day = str(ENDTIME.day)
    filename = ENDTIME.strftime('%Y%m%dT%H%M%S') + ".png"
    script_loc = os.path.dirname(__file__)
    img_base = os.path.join(script_loc, 'spectrograms/static/plots')

    from station_config import locations

    procs = []
    with ProcessPoolExecutor(initializer = init_generation,
                             initargs = (config, )) as executor:
        for loc, stations in locations.items():
            path = os.path.join(img_base, loc, year, month, day)
            os.makedirs(path, exist_ok = True)
            filepath = os.path.join(path, filename)

            future = executor.submit(generate_spectrogram, filepath, stations, STARTTIME, ENDTIME)
            procs.append(future)

    for proc in procs:
        print(proc.exception())


def save_csv(station, times, z_data, n_data, e_data):
    os.makedirs('CSVFiles', exist_ok = True)
    end_time = pandas.to_datetime(str(times[-1])).strftime("%Y_%m_%d_%H_%M_%S")
    csv_filename = f"CSVFiles/{station}_{end_time}.csv"
    data = zip(times, z_data, n_data, e_data)
    with open(csv_filename, 'w') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerows(data)


def generate_spectrogram(filename, stations, STARTTIME, ENDTIME):
    # Create a plot figure to hold the waveform and spectrogram graphs
    plot_height = 1.52 * len(stations)
    plot_width = 5.76
    num_plots = 2 * len(stations)
    ratios = [3, 10] * len(stations)
    dpi = 100

    plt.rcParams.update({'font.size': 7})
    fig = plt.figure(dpi = dpi, figsize = (plot_width, plot_height))

    gs = fig.add_gridspec(num_plots, hspace = 0, height_ratios = ratios)
    axes = gs.subplots(sharex = True)

    # Get some config variables
    meta_base_url = CONFIG['IRIS']['url']
    winston_url = CONFIG['WINSTON']['url']
    winston_port = CONFIG['WINSTON'].getint('port', 16022)

    PAD = 10

    # filter parameters
    low = CONFIG['FILTER'].getfloat('lowcut', 0.5)
    high = CONFIG['FILTER'].getfloat('highcut', 15)
    order = CONFIG['FILTER'].getint('order', 2)

    # spectrogram parameters
    window_type = CONFIG['SPECTROGRAM']['WindowType']
    window_size = CONFIG['SPECTROGRAM'].getint('WindowSize')
    overlap = CONFIG['SPECTROGRAM'].getint('Overlap')
    NFFT = CONFIG['SPECTROGRAM'].getint('NFFT')

    # Spectrogram graph range display
    min_freq = CONFIG['SPECTROGRAM'].getint('MinFreq', 0)
    max_freq = CONFIG['SPECTROGRAM'].getint('MaxFreq', 10)

    wclient = WClient(winston_url, winston_port)

    # Generate a linear normilization for the spectrogram.
    # Values here are arbitrary, just what happened to work in testing.
    norm = Normalize(-360, -180)

    cm = generate_colormap()

    for idx, sta_dict in enumerate(stations):
        STA = sta_dict.get('STA')
        CHAN = sta_dict.get('CHAN', 'BHZ')
        NET = sta_dict.get('NET', 'AV')
        station = f"{STA}.{CHAN}"

        # Configure the plot for this station
        ax_idx = 2 * idx
        ax1 = axes[ax_idx]
        ax2 = axes[ax_idx + 1]

        ax1.set_yticks([])  # No y labels on waveform plot

        ticklen = 8

        ax2.set_ylim([min_freq, max_freq])
        ax2.set_yticks(numpy.arange(min_freq, max_freq, 2))  # Mark even values of frequency
        ax2.set_ylabel(station)  # Add the station label
        ax2.xaxis.set_tick_params(direction='inout', bottom = True,
                                  top = True, length = ticklen)
        ax2.yaxis.set_tick_params(direction = "in", right = True)

        direction = "inout"
        if idx == 0:
            direction = "in"
            ticklen /= 2

        ax1.xaxis.set_tick_params(direction = direction, bottom = True,
                                  top = True, length = ticklen)
        ax1.yaxis.set_tick_params(left = False)

        # Get the data for this station from the winston server
        CHAN_WILD = CHAN[:-1] + '*'
        stream = wclient.get_waveforms(
            NET, STA, '--', CHAN_WILD,
            STARTTIME - PAD,
            ENDTIME + PAD,
            cleanup=True
        )
        if stream.count() == 0:
            # TODO: Make note of no data for this station/time range, and check again later
            continue  # No data for this station

        # Get the actual start time from the data, in case it's
        # slightly different from what we requested.
        DATA_START = UTCDateTime(stream[0].stats['starttime'])
        if stream[0].count() < window_size:
            # Not enough data to work with
            continue

        # Get the meta data for this station/channel from IRIS
        meta_url = f'{meta_base_url}net={NET}&sta={STA}&cha={CHAN_WILD}&starttime={STARTTIME-PAD}&endtime={ENDTIME+PAD}&level=channel&format=text'
        resp = requests.get(meta_url)

        resp_str = StringIO(resp.text)
        reader = csv.reader(resp_str, delimiter = '|')
        keys = [x.strip() for x in next(reader)]
        meta = {}
        for line in reader:
            channel = line[3]
            meta[channel] = dict(zip(keys, line))

        resp_str.close()

        # Create an array of timestamps corresponding to the data points
        waveform_times = stream[0].times()
        waveform_times = ((waveform_times + DATA_START.timestamp) * 1000).astype('datetime64[ms]')

        # What it says
        stream.detrend()

        # Apply a butterworth bandpass filter to get rid of some noise
        stream.filter('bandpass', freqmin = low, freqmax = high,
                      corners = order, zerophase = True)

        for trace in stream:
            channel = trace.stats.channel
            scale = int(float(meta[channel]['Scale']))
            trace.data /= scale
            trace.data = trace.data - trace.data.mean()

        # Get the raw z data as a numpy array
        z_tr = stream.select(component = 'Z').pop()
        z_channel = z_tr.stats.channel
        z_data = z_tr.data

        try:
            n_data = stream.select(component = 'N').pop().data
            e_data = stream.select(component = 'E').pop().data
        except Exception as e:
            print(e)
        else:
            save_csv(STA, waveform_times, z_data, n_data, e_data)

        # Generate the parameters/data for a spectrogram
        sample_rate = float(meta[z_channel]['SampleRate'])
        spec_info = spectrogram(z_data, sample_rate, window_type, nperseg = window_size,
                                noverlap = overlap, nfft = NFFT)

        # Convert the times returned from the spectrogram function (0-600 seconds)
        # to real timestamps to line up with the waveform.
        spectrograph_times = spec_info[1]
        spectrograph_times = (spectrograph_times + DATA_START.timestamp).astype('datetime64[s]')

        # Plot the waveform
        ax1.plot(waveform_times, z_data, 'k-', linewidth = .5)
        ax1.set_ylim([-3.2e-5, 3.2e-5])

        # And the spectrogram
        ax2.pcolormesh(
            spectrograph_times, spec_info[0], 20 * numpy.log10(numpy.abs(spec_info[2])),
            norm = norm, cmap = cm, shading = "auto"
        )

        ax2.set_xlim(STARTTIME, ENDTIME)  # Expand x axes to the full requested range

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))  # Format dates as hour:minute
    # TODO: save plot image full-size and thumbnail
    side_padding = 25 / (plot_width * dpi)
    bottom_padding = 25 / (plot_height * dpi)
    fig.tight_layout(pad = 0, rect = (side_padding, bottom_padding,
                                      1 - side_padding, 1))

    fig.savefig(filename)
    print(filename)
    gen_thumbnail(filename, fig)


def gen_thumbnail(filename, fig):
    small_path = list(os.path.split(filename))
    small_path[-1] = "small_" + small_path[-1]
    filename = os.path.join(*small_path)
    axes = fig.axes
    for ax in axes:
        ax.xaxis.set_tick_params(top = False, bottom = False)
        ax.yaxis.set_tick_params(left = False, right = False)
        ax.set_ylabel("")
        ax.axis("off")

    thumb_height = .396 * (len(axes) / 2)
    thumb_width = 1.5
    fig.set_size_inches(thumb_width, thumb_height)
    fig.tight_layout(pad = 0)

    fig.savefig(filename, transparent = False, pad_inches=0)


if __name__ == "__main__":
    main()
