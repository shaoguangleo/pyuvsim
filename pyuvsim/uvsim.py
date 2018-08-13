# -*- mode: python; coding: utf-8 -*
# Copyright (c) 2018 Radio Astronomy Software Group
# Licensed under the 2-clause BSD License

# CLEANUP: standard packages at the top, related next, then package specific imports

from __future__ import absolute_import, division, print_function

import numpy as np
import os
import sys
from itertools import izip
import astropy.constants as const
import astropy.units as units
from astropy.units import Quantity
from astropy.time import Time
from astropy.coordinates import EarthLocation
from pyuvdata import UVData, UVBeam
import pyuvdata.utils as uvutils
from .mpi import comm, rank, Npus, set_mpi_excepthook
from . import profiling
from .antenna import Antenna
from .baseline import Baseline
from .telescope import Telescope
from . import utils as simutils
from . import simsetup

__all__ = ['UVTask', 'UVEngine', 'uvdata_to_task_list', 'run_uvsim', 'initialize_uvdata', 'serial_gather']
 
# CLEANUP: find more streamlined way to import progressbar.  
# CLEANUP: Should only appear in whatever file actually does the running of the code.
try:
    import progressbar
    progbar = True
except(ImportError):
    progbar = False

progsteps = False
try:
    if os.environ['PYUVSIM_BATCH_JOB'] == '1':
        progsteps = True
        progbar = False
except(KeyError):
    progbar = progbar

set_mpi_excepthook(comm)


@profile
class UVTask(object):
    # holds all the information necessary to calculate a single src, t, f, bl, array
    # need the array because we need an array location for mapping to locat az/za

    def __init__(self, source, time, freq, baseline, telescope):
        self.time = time
        self.freq = freq
        self.source = source
        self.baseline = baseline
        self.telescope = telescope
        self.visibility_vector = None
        self.uvdata_index = None        # Where to add the visibility in the uvdata object.

    def __eq__(self, other):
        return (np.isclose(self.time, other.time, atol=1e-4)
                and np.isclose(self.freq, other.freq, atol=1e-4)
                and (self.source == other.source)
                and (self.baseline == other.baseline)
                and (self.visibility_vector == other.visibility_vector)
                and (self.uvdata_index == other.uvdata_index)
                and (self.telescope == other.telescope))

    def __gt__(self, other):
        blti0, _, fi0 = self.uvdata_index
        blti1, _, fi1 = other.uvdata_index
        if self.baseline == other.baseline:
            if fi0 == fi1:
                return blti0 > blti1
            return fi0 > fi1
        return self.baseline > other.baseline

    def __ge__(self, other):
        blti0, _, fi0 = self.uvdata_index
        blti1, _, fi1 = other.uvdata_index
        if self.baseline == other.baseline:
            if fi0 == fi1:
                return blti0 >= blti1
            return fi0 >= fi1
        return self.baseline >= other.baseline

    def __lt__(self, other):
        return not self.__ge__(other)

    def __le__(self, other):
        return not self.__gt__(other)


class UVEngine(object):

    def __init__(self, task):   # task_array  = list of tuples (source,time,freq,uvw)
        # self.rank
        self.task = task
        # Time and freq are scattered as floats.
        # Convert them to astropy Quantities
        if isinstance(self.task.time, float):
            self.task.time = Time(self.task.time, format='jd')
        if isinstance(self.task.freq, float):
            self.task.freq = self.task.freq * units.Hz

    def apply_beam(self):
        """ Get apparent coherency from jones matrices and source coherency. """
        baseline = self.task.baseline
        source = self.task.source
        # coherency is a 2x2 matrix
        # [ |Ex|^2, Ex* Ey, Ey* Ex |Ey|^2 ]
        # where x and y vectors along the local za/az axes.

        # Apparent coherency gives the direction and polarization dependent baseline response to a source.
        beam1_jones = baseline.antenna1.get_beam_jones(self.task.telescope,
                                                       source.az_za_calc(self.task.time,
                                                                         self.task.telescope.telescope_location),
                                                       self.task.freq)
        beam2_jones = baseline.antenna2.get_beam_jones(self.task.telescope,
                                                       source.az_za_calc(self.task.time,
                                                                         self.task.telescope.telescope_location),
                                                       self.task.freq)
        this_apparent_coherency = np.dot(beam1_jones,
                                         source.coherency_calc(self.task.time,
                                                               self.task.telescope.telescope_location))
        this_apparent_coherency = np.dot(this_apparent_coherency,
                                         (beam2_jones.conj().T))

        self.apparent_coherency = this_apparent_coherency

    def make_visibility(self):
        """ Visibility contribution from a single source """
        assert(isinstance(self.task.freq, Quantity))
        self.apply_beam()

        pos_lmn = self.task.source.pos_lmn(self.task.time, self.task.telescope.telescope_location)
        if pos_lmn is None:
            return np.array([0., 0., 0., 0.], dtype=np.complex128)

        # need to convert uvws from meters to wavelengths
        uvw_wavelength = self.task.baseline.uvw / const.c * self.task.freq.to('1/s')
        fringe = np.exp(2j * np.pi * np.dot(uvw_wavelength, pos_lmn))
        vij = self.apparent_coherency * fringe

        # Reshape to be [xx, yy, xy, yx]
        vis_vector = [vij[0, 0], vij[1, 1], vij[0, 1], vij[1, 0]]
        return np.array(vis_vector)

    def update_task(self):
        self.task.visibility_vector = self.make_visibility()



def uvdata_to_task_list(input_uv, sources, beam_list, beam_dict=None):
    """Create task list from pyuvdata compatible input file.

    Returns: List of task parameters to be send to UVEngines
    List has task parameters defined in UVTask object
    This function extracts time, freq, Antenna1, Antenna2
    """
    if not isinstance(input_uv, UVData):
        raise TypeError("input_uv must be UVData object")

    if not isinstance(sources, np.ndarray):
        raise TypeError("sources must be a numpy array")

    freq = input_uv.freq_array[0, :]  # units.Hz

    telescope = Telescope(input_uv.telescope_name,
                          EarthLocation.from_geocentric(*input_uv.telescope_location, unit='m'),
                          beam_list)

    if len(beam_list) > 1 and beam_dict is None:
        raise ValueError('beam_dict must be supplied if beam_list has more than one element.')

    times = input_uv.time_array

    antpos_ENU, _ = input_uv.get_ENU_antpos()

    antenna_names = input_uv.antenna_names
    antennas = []
    for num, antname in enumerate(antenna_names):
        if beam_dict is None:
            beam_id = 0
        else:
            beam_id = beam_dict[antname]
        antennas.append(Antenna(antname, num, antpos_ENU[num], beam_id))

    baselines = []
    print('Generating Baselines')
    for count, antnum1 in enumerate(input_uv.ant_1_array):
        antnum2 = input_uv.ant_2_array[count]
        index1 = np.where(input_uv.antenna_numbers == antnum1)[0][0]
        index2 = np.where(input_uv.antenna_numbers == antnum2)[0][0]
        baselines.append(Baseline(antennas[index1], antennas[index2]))

    baselines = np.array(baselines)

    blts_index = np.arange(input_uv.Nblts)
    frequency_index = np.arange(input_uv.Nfreqs)
    source_index = np.arange(len(sources))
    print('Making Meshgrid')
    blts_ind, freq_ind, source_ind = np.meshgrid(blts_index, frequency_index, source_index)
    print('Raveling')
    blts_ind = blts_ind.ravel()
    freq_ind = freq_ind.ravel()
    source_ind = source_ind.ravel()

    uvtask_list = []
    print('Making Tasks')
    print('Number of tasks:', len(blts_ind))

    if progsteps or progbar:
        count = 0
        tot = len(blts_ind)
        if progbar:
            pbar = progressbar.ProgressBar(maxval=tot).start()
        else:
            pbar = utils.progsteps(maxval=tot)

    for (bl, freqi, t, source, blti, fi) in izip(baselines[blts_ind],
                                                 freq[freq_ind], times[blts_ind],
                                                 sources[source_ind], blts_ind,
                                                 freq_ind):
        task = UVTask(source, t, freqi, bl, telescope)
        task.uvdata_index = (blti, 0, fi)    # 0 = spectral window index
        uvtask_list.append(task)

        if progbar or progsteps:
            count += 1
            pbar.update(count)

    if progbar:
        pbar.finish()
    return uvtask_list


def initialize_uvdata(uvtask_list, source_list_name, uvdata_file=None,
                      obs_param_file=None, telescope_config_file=None,
                      antenna_location_file=None):
    """
    Initialize an empty uvdata object to fill with simulation.

    Args:
        uvtask_list: List of uvtasks to simulate.
        source_list_name: Name of source list file or mock catalog.
        uvdata_file: Name of input UVData file or None if initializing from
            config files.
        obs_param_file: Name of observation parameter config file or None if
            initializing from a UVData file.
        telescope_config_file: Name of telescope config file or None if
            initializing from a UVData file.
        antenna_location_file: Name of antenna location file or None if
            initializing from a UVData file.
    """

    if not isinstance(source_list_name, str):
        raise ValueError('source_list_name must be a string')

    if uvdata_file is not None:
        if not isinstance(uvdata_file, str):
            raise ValueError('uvdata_file must be a string')
        if (obs_param_file is not None or telescope_config_file is not None
                or antenna_location_file is not None):
            raise ValueError('If initializing from a uvdata_file, none of '
                             'obs_param_file, telescope_config_file or '
                             'antenna_location_file can be set.')
    elif (obs_param_file is None or telescope_config_file is None
            or antenna_location_file is None):
        if not isinstance(obs_param_file, str):
            raise ValueError('obs_param_file must be a string')
        if not isinstance(telescope_config_file, str):
            raise ValueError('telescope_config_file must be a string')
        if not isinstance(antenna_location_file, str):
            raise ValueError('antenna_location_file must be a string')
        raise ValueError('If not initializing from a uvdata_file, all of '
                         'obs_param_file, telescope_config_file or '
                         'antenna_location_file must be set.')

    # Version string to add to history
    history = simutils.get_version_string()

    history += ' Sources from source list: ' + source_list_name + '.'

    if uvdata_file is not None:
        history += ' Based on UVData file: ' + uvdata_file + '.'
    else:
        history += (' Based on config files: ' + obs_param_file + ', '
                    + telescope_config_file + ', ' + antenna_location_file)

    history += ' Npus = ' + str(Npus) + '.'

    task_freqs = []
    task_bls = []
    task_times = []
    task_antnames = []
    task_antnums = []
    task_antpos = []
    task_uvw = []
    ant_1_array = []
    ant_2_array = []
    telescope_name = uvtask_list[0].telescope.telescope_name
    telescope_location = uvtask_list[0].telescope.telescope_location.geocentric

    source_0 = uvtask_list[0].source
    freq_0 = uvtask_list[0].freq
    for task in uvtask_list:
        if not task.source == source_0:
            continue
        task_freqs.append(task.freq)

        if task.freq == freq_0:
            task_bls.append(task.baseline)
            task_times.append(task.time)
            task_antnames.append(task.baseline.antenna1.name)
            task_antnames.append(task.baseline.antenna2.name)
            ant_1_array.append(task.baseline.antenna1.number)
            ant_2_array.append(task.baseline.antenna2.number)
            task_antnums.append(task.baseline.antenna1.number)
            task_antnums.append(task.baseline.antenna2.number)
            task_antpos.append(task.baseline.antenna1.pos_enu)
            task_antpos.append(task.baseline.antenna2.pos_enu)
            task_uvw.append(task.baseline.uvw)

    antnames, ant_indices = np.unique(task_antnames, return_index=True)
    task_antnums = np.array(task_antnums)
    task_antpos = np.array(task_antpos)
    antnums = task_antnums[ant_indices]
    antpos = task_antpos[ant_indices]

    freqs = np.unique(task_freqs)

    uv_obj = UVData()

    # add pyuvdata version info
    history += uv_obj.pyuvdata_version_str

    uv_obj.telescope_name = telescope_name
    uv_obj.telescope_location = np.array([tl.to('m').value for tl in telescope_location])
    uv_obj.instrument = telescope_name
    uv_obj.Nfreqs = freqs.size
    uv_obj.Ntimes = np.unique(task_times).size
    uv_obj.Nants_data = antnames.size
    uv_obj.Nants_telescope = uv_obj.Nants_data
    uv_obj.Nblts = len(ant_1_array)

    uv_obj.antenna_names = antnames.tolist()
    uv_obj.antenna_numbers = antnums
    antpos_ecef = uvutils.ECEF_from_ENU(antpos, *uv_obj.telescope_location_lat_lon_alt) - uv_obj.telescope_location
    uv_obj.antenna_positions = antpos_ecef
    uv_obj.ant_1_array = np.array(ant_1_array)
    uv_obj.ant_2_array = np.array(ant_2_array)
    uv_obj.time_array = np.array(task_times)
    uv_obj.uvw_array = np.array(task_uvw)
    uv_obj.baseline_array = uv_obj.antnums_to_baseline(ant_1_array, ant_2_array)
    uv_obj.Nbls = np.unique(uv_obj.baseline_array).size
    if uv_obj.Nfreqs == 1:
        uv_obj.channel_width = 1.  # Hz
    else:
        uv_obj.channel_width = np.diff(freqs)[0]

    if uv_obj.Ntimes == 1:
        uv_obj.integration_time = np.ones_like(uv_obj.time_array, dtype=np.float64)  # Second
    else:
        # Note: currently only support a constant spacing of times
        uv_obj.integration_time = (np.ones_like(uv_obj.time_array, dtype=np.float64)
                                   * np.diff(np.unique(task_times))[0])
    uv_obj.set_lsts_from_time_array()
    uv_obj.zenith_ra = uv_obj.lst_array
    uv_obj.zenith_dec = np.repeat(uv_obj.telescope_location_lat_lon_alt[0], uv_obj.Nblts)  # Latitude
    uv_obj.object_name = 'zenith'
    uv_obj.set_drift()
    uv_obj.vis_units = 'Jy'
    uv_obj.polarization_array = np.array([-5, -6, -7, -8])
    uv_obj.spw_array = np.array([0])
    uv_obj.freq_array = np.array([freqs])

    uv_obj.Nspws = uv_obj.spw_array.size
    uv_obj.Npols = uv_obj.polarization_array.size

    uv_obj.data_array = np.zeros((uv_obj.Nblts, uv_obj.Nspws, uv_obj.Nfreqs, uv_obj.Npols), dtype=np.complex)
    uv_obj.flag_array = np.zeros((uv_obj.Nblts, uv_obj.Nspws, uv_obj.Nfreqs, uv_obj.Npols), dtype=bool)
    uv_obj.nsample_array = np.ones_like(uv_obj.data_array, dtype=float)
    uv_obj.history = history

    uv_obj.check()

    return uv_obj


def serial_gather(uvtask_list, uv_out):
    """
        Initialize uvdata object, loop over uvtask list, acquire visibilities,
        and add to uvdata object.
    """
    for task in uvtask_list:
        blt_ind, spw_ind, freq_ind = task.uvdata_index
        uv_out.data_array[blt_ind, spw_ind, freq_ind, :] += task.visibility_vector

    return uv_out


def run_uvsim(input_uv, beam_list, beam_dict=None, catalog_file=None,
              mock_keywords=None,
              uvdata_file=None, obs_param_file=None,
              telescope_config_file=None, antenna_location_file=None):
    """
    Run uvsim

    Arguments:
        input_uv: An input UVData object, containing baseline/time/frequency information.
        beam_list: A list of UVBeam and/or AnalyticBeam objects

    Keywords:
        beam_dict: Dictionary of {antenna_name : beam_ID}, where beam_id is an index in
                   the beam_list. This assigns beams to antennas.
                   Default: All antennas get the 0th beam in the beam_list.
        catalog_file: Catalog file name.
                   Default: Create a mock catalog
        mock_keywords: Settings for a mock catalog (see keywords of create_mock_catalog)
        uvdata_file: Name of input UVData file if running from a file.
        obs_param_file: Parameter filename if running from config files.
        telescope_config_file: Telescope configuration file if running from config files.
        antenna_location_file: antenna_location file if running from config files.
    """
    if not isinstance(input_uv, UVData):
        raise TypeError("input_uv must be UVData object")
    # The Head node will initialize our simulation
    # Read input file and make uvtask list
    uvtask_list = []
    if rank == 0:
        print('Nblts:', input_uv.Nblts)
        print('Nfreqs:', input_uv.Nfreqs)

        if catalog_file is None or catalog_file == 'mock':
            # time, arrangement, array_location, save, Nsrcs, max_za

            if mock_keywords is None:
                mock_keywords = {}

            if 'array_location' not in mock_keywords:
                array_loc = EarthLocation.from_geocentric(*input_uv.telescope_location, unit='m')
                mock_keywords['array_location'] = array_loc
            if 'time' not in mock_keywords:
                mock_keywords['time'] = input_uv.time_array[0]

            if "array_location" not in mock_keywords:
                print("Warning: No array_location given for mock catalog. Defaulting to HERA site")
            if 'time' not in mock_keywords:
                print("Warning: No julian date given for mock catalog. Defaulting to first of input_UV object")

            time = mock_keywords.pop('time')

            catalog, mock_keywords = simsetup.create_mock_catalog(time, **mock_keywords)

            mock_keyvals = [str(key) + str(val) for key, val in mock_keywords.iteritems()]
            source_list_name = 'mock_' + "_".join(mock_keyvals)
        elif isinstance(catalog_file, str):
            source_list_name = catalog_file
            if catalog_file.endswith("txt"):
                catalog = simsetup.point_sources_from_params(catalog_file)
            elif catalog_file.endswith('vot'):
                catalog = simsetup.read_gleam_catalog(catalog_file)

        catalog = np.array(catalog)
        print('Nsrcs:', len(catalog))
        uvtask_list = uvdata_to_task_list(input_uv, catalog, beam_list, beam_dict=beam_dict)

        if 'obs_param_file' in input_uv.extra_keywords:
            obs_param_file = input_uv.extra_keywords['obs_param_file']
            telescope_config_file = input_uv.extra_keywords['telescope_config_file']
            antenna_location_file = input_uv.extra_keywords['antenna_location_file']
            uvdata_file_pass = None
        else:
            uvdata_file_pass = uvdata_file

        uv_container = initialize_uvdata(uvtask_list, source_list_name,
                                         uvdata_file=uvdata_file_pass,
                                         obs_param_file=obs_param_file,
                                         telescope_config_file=telescope_config_file,
                                         antenna_location_file=antenna_location_file)

        # To split into PUs make a list of lists length NPUs
        print("Splitting Task List")
        uvtask_list = np.array_split(uvtask_list, Npus)
        uvtask_list = [list(tl) for tl in uvtask_list]

        print("Sending Tasks To Processing Units")
        sys.stdout.flush()
    # Scatter the task list among all available PUs
    local_task_list = comm.scatter(uvtask_list, root=0)
    if rank == 0:
        print("Tasks Received. Begin Calculations.")
        sys.stdout.flush()

    # UVBeam objects don't survive the scatter with prop_fget() working. This fixes it on each rank.
    for i, bm in enumerate(local_task_list[0].telescope.beam_list):
        if isinstance(bm, UVBeam):
            uvb = UVBeam()
            uvb = bm
            local_task_list[0].telescope.beam_list[i] = bm

    summed_task_dict = {}

    if rank == 0:
        if progsteps or progbar:
            count = 0
            tot = len(local_task_list)
            print("Local tasks: ", tot)
            sys.stdout.flush()
            if progbar:
                pbar = progressbar.ProgressBar(maxval=tot).start()
            else:
                pbar = utils.progsteps(maxval=tot)

        for count, task in enumerate(local_task_list):
            engine = UVEngine(task)
            if task.uvdata_index not in summed_task_dict.keys():
                summed_task_dict[task.uvdata_index] = task
            if summed_task_dict[task.uvdata_index].visibility_vector is None:
                summed_task_dict[task.uvdata_index].visibility_vector = engine.make_visibility()
            else:
                summed_task_dict[task.uvdata_index].visibility_vector += engine.make_visibility()

            if progbar or progsteps:
                pbar.update(count)

        if progbar or progsteps:
            pbar.finish()
    else:
        for task in local_task_list:
            engine = UVEngine(task)
            if task.uvdata_index not in summed_task_dict.keys():
                summed_task_dict[task.uvdata_index] = task
            if summed_task_dict[task.uvdata_index].visibility_vector is None:
                summed_task_dict[task.uvdata_index].visibility_vector = engine.make_visibility()
            else:
                summed_task_dict[task.uvdata_index].visibility_vector += engine.make_visibility()

    if rank == 0:
        print("Calculations Complete.")

    # All the sources in this summed list are foobar-ed
    # Source are summed over but only have 1 name
    # Some source may be correct
    summed_local_task_list = summed_task_dict.values()
    # gather all the finished local tasks into a list of list of len NPUs
    # gather is a blocking communication, have to wait for all PUs
    full_tasklist = comm.gather(summed_local_task_list, root=0)

    # Concatenate the list of lists into a flat list of tasks
    if rank == 0:
        uvtask_list = sum(full_tasklist, [])
        uvdata_out = serial_gather(uvtask_list, uv_container)

        return uvdata_out
