# -*- mode: python; coding: utf-8 -*
# Copyright (c) 2018 Radio Astronomy Software Group
# Licensed under the 3-clause BSD License

from __future__ import absolute_import, division, print_function

import numpy as np
from numpy.lib import recfunctions
import yaml
import os
import warnings
import six
import ast
import copy
from six.moves import map, range, zip
import astropy.units as units
from astropy.time import Time
from astropy.io.votable import parse
from astropy.coordinates import Angle, SkyCoord, EarthLocation, AltAz

from pyuvdata import UVBeam, UVData
import pyuvdata.utils as uvutils

from .source import SkyModel
from .analyticbeam import AnalyticBeam
from .mpi import get_rank
from .utils import check_file_exists_and_increment
from pyuvsim.data import DATA_PATH as SIM_DATA_PATH


def _parse_layout_csv(layout_csv):
    """ Interpret the layout csv file """

    with open(layout_csv, 'r') as fhandle:
        header = fhandle.readline()

    header = [h.strip() for h in header.split()]
    if six.PY2:
        str_format_code = 'a'
    else:
        str_format_code = 'U'

    # get data types for each column
    dtypes = {"name": str_format_code + '10', 'number': 'i4', 'beamid': 'i4', 'e': 'f8', 'n': 'f8', 'u': 'f8'}

    # check columns in file
    lower_header = [col.lower() for col in header]
    columns = ['name', 'number', 'beamid', 'e', 'n', 'u']
    col_exist = [col for col in columns if col in lower_header]

    dt = np.format_parser([dtypes[col] for col in col_exist],
                          col_exist, header)

    return np.genfromtxt(layout_csv, autostrip=True, skip_header=1,
                         dtype=dt.dtype)


def _write_layout_csv(filepath, antpos_enu, antenna_names, antenna_numbers):
    col_width = max([len(name) for name in antenna_names])
    header = ("{:" + str(col_width) + "} {:8} {:8} {:10} {:10} {:10}\n").format("Name", "Number", "BeamID", "E", "N", "U")
    with open(filepath, 'w') as lfile:
        lfile.write(header + '\n')
        for i, (e, n, u) in enumerate(antpos_enu):
            beam_id = 0
            name = antenna_names[i]
            num = antenna_numbers[i]
            line = ("{:" + str(col_width) + "} {:8d} {:8d} {:10.4f} {:10.4f} {:10.4f}\n").format(name, num, beam_id, e, n, u)
            lfile.write(line)


def _config_str_to_dict(config_str):
    """ Read yaml file and add paths to dictionary """

    with open(config_str, 'r') as pfile:
        param_dict = yaml.safe_load(pfile)

    param_dict['config_path'] = os.path.dirname(config_str)
    param_dict['param_file'] = os.path.basename(config_str)

    return param_dict


def skymodel_to_array(sky):
    """
    Return a recarray of source components from a given SkyModel object.
    """

    dt = np.format_parser(['U10', 'f8', 'f8', 'f8', 'f8'], ['source_id', 'ra_j2000', 'dec_j2000', 'flux_density_I', 'frequency'], [])

    arr = np.empty(sky.Ncomponents, dtype=dt.dtype)
    arr['source_id'] = sky.name
    arr['ra_j2000'] = sky.ra.value
    arr['dec_j2000'] = sky.dec.value
    arr['flux_density_I'] = sky.stokes[0, :]
    arr['frequency'] = sky.freq

    return arr


def array_to_skymodel(catalog_table):
    """
    Make a SkyModel object from a recarray.

    """

    ra = Angle(catalog_table['ra_j2000'], units.deg)
    dec = Angle(catalog_table['dec_j2000'], units.deg)
    ids = catalog_table['source_id']
    freqs = catalog_table['frequency'] * units.Hz
    flux_I = np.atleast_1d(catalog_table['flux_density_I'])
    stokes = np.pad(np.expand_dims(flux_I, 1), ((0, 0), (0, 3)), 'constant').T
    rise_lst = None
    set_lst = None
    if 'rise_lst' in catalog_table.dtype.names:
        rise_lst = catalog_table['rise_lst']
        set_lst = catalog_table['set_lst']
    sourcelist = SkyModel(ids, ra, dec, freqs, stokes, rise_lst=rise_lst, set_lst=set_lst)

    return sourcelist


def source_cuts(catalog_table, input_uv=None, lst_array=None, latitude_deg=None, horizon_buffer=0.04364, min_flux=None, max_flux=None):
    """
    Performing flux and horizon selections on recarray of source components.

    Args:
        catalog_table: recarray of source catalog information. Must have columns with names
                       'source_id', 'ra_j2000', 'dec_j2000', 'flux_density_I', 'frequency'
        input_uv: (UVData)
            If not None, can set lst_array and latitude_deg if either or both is missing.
        lst_array: For coarse RA horizon cuts, lsts used in the simulation [radians]
        latitude_deg: Latitude of telescope in degrees. Used for declination coarse horizon cut.
        horizon_buffer: Angle (float, in radians) of buffer for coarse horizon cut. Default is about 10 minutes of sky rotation.
                        SkyModel whose calculated altitude is less than -horizon_buffer are excluded by the catalog read.

                        Caution! The altitude calculation does not account for precession/nutation of the Earth. The buffer angle
                        is needed to ensure that the horizon cut doesn't exclude sources near but above the horizon.
                        Since the cutoff is done using lst, and the lsts are calculated with astropy, the required buffer should
                        _not_ drift with time since the J2000 epoch. The default buffer has been tested around julian date 2457458.0.

        min_flux: Minimum stokes I flux to select [Jy]
        max_flux: Maximum stokes I flux to select [Jy]
    Returns:
        A new recarray of source components, with additional columns for rise and set lst.

    """

    # Option to pass a UVData object to get telescope-specific parameters
    if input_uv:
        if lst_array is None:
            lst_array, inds = np.unique(input_uv.lst_array, return_index=True)
        if latitude_deg is None:
            latitude_deg = input_uv.telescope_location_lat_lon_alt_degrees[0]

    sourcelist = []
    Nsrcs = catalog_table.shape[0]
    coarse_kwds = [lst_array, latitude_deg]
    coarse_horizon_cut = all([k is not None for k in coarse_kwds])

    if (not coarse_horizon_cut) and any([k is not None for k in coarse_kwds]):
        warnings.warn("It looks like you want to do a coarse horizon cut, but you're missing keywords!")

    if coarse_horizon_cut:
        lat_rad = np.radians(latitude_deg)
        buff = horizon_buffer

    if min_flux:
        catalog_table = catalog_table[catalog_table['flux_density_I'] > min_flux]

    if max_flux:
        catalog_table = catalog_table[catalog_table['flux_density_I'] < max_flux]

    ra = Angle(catalog_table['ra_j2000'], units.deg)
    dec = Angle(catalog_table['dec_j2000'], units.deg)

    if coarse_horizon_cut:
        tans = np.tan(lat_rad) * np.tan(dec.rad)
        nonrising = tans < -1

        catalog_table = catalog_table[~nonrising]
        tans = tans[~nonrising]

        ra = ra[~nonrising]
        dec = dec[~nonrising]

    ids = catalog_table['source_id']
    freqs = catalog_table['frequency']

    if coarse_horizon_cut:
        circumpolar = tans >= 1      # These will have rise/set lst set to nan
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            rise_lst = ra.rad - np.arccos((-1) * tans) - buff
            set_lst = ra.rad + np.arccos((-1) * tans) + buff
            rise_lst[rise_lst < 0] += 2 * np.pi
            set_lst[set_lst < 0] -= 2 * np.pi

        catalog_table = recfunctions.append_fields(catalog_table, ['rise_lst', 'set_lst'], [rise_lst, set_lst], usemask=False)

    return catalog_table


def read_votable_catalog(gleam_votable, input_uv=None, source_select_kwds={}, return_table=False):
    """
    Creates a list of pyuvsim source objects from a votable catalog.

    Args:
        gleam_votable: Path to votable catalog file.
        input_uv: The UVData object for the simulation (needed for horizon cuts)
        return_table: If True, return the astropy table instead of a list of Source objects. (Default False)
        source_select_kwds: Dictionary of keywords for source selection
            Valid options:
            |  lst_array: For coarse RA horizon cuts, lsts used in the simulation [radians]
            |  latitude_deg: Latitude of telescope in degrees. Used for declination coarse horizon cut.
            |  horizon_buffer: Angle (float, in radians) of buffer for coarse horizon cut. Default is about 10 minutes of sky rotation.
            |                  (See caveats in simsetup.array_to_skymodel docstring)
            |  min_flux: Minimum stokes I flux to select [Jy]
            |  max_flux: Maximum stokes I flux to select [Jy]

    Returns:
        if return_table : recarray of source parameters
        otherwise:
            pyuvsim.SkyModel object

    Tested on: GLEAM EGC catalog, version 2
    """
    class Found(Exception):
        pass

    resources = parse(gleam_votable).resources
    try:
        for rs in resources:
            for tab in rs.tables:
                if 'GLEAM' in tab.array.dtype.names:
                    raise Found
    except Found:
        table = tab.to_table()      # Convert to astropy Table

    fieldnames = ['GLEAM', 'RAJ2000', 'DEJ2000', 'Fintwide']
    newnames = ['source_id', 'ra_j2000', 'dec_j2000', 'flux_density_I', 'frequency']
    data = table[fieldnames]
    Nsrcs = len(data)
    freq = 200e6
    for t in data.colnames:
        i = fieldnames.index(t)
        data[t] = data[t]
        data[t].name = newnames[i]
    data.add_column(table.Column(np.ones(Nsrcs) * freq, name='frequency'))
    data = data.as_array().data

    if len(source_select_kwds) > 0:
        data = source_cuts(data, input_uv=input_uv, **source_select_kwds)

    if return_table:
        return data

    return array_to_skymodel(data)


def read_text_catalog(catalog_csv, input_uv=None, source_select_kwds={}, return_table=False):
    """
    Read in a text file of sources.

    Args:
        catalog_csv: csv file with the following expected columns:
            |  Source_ID: source id as a string of maximum 10 characters
            |  ra_j2000: right ascension at J2000 epoch, in decimal degrees
            |  dec_j2000: declination at J2000 epoch, in decimal degrees
            |  flux_density_I: Stokes I flux density in Janskys
            |  frequency: reference frequency (for future spectral indexing) [Hz]
                For now, all sources are flat spectrum.
        input_uv: The UVData object for the simulation (needed for horizon cuts)
        source_select_kwds: Dictionary of keywords for source selection.
            Valid options:
            |  lst_array: For coarse RA horizon cuts, lsts used in the simulation [radians]
            |  latitude_deg: Latitude of telescope in degrees. Used for declination coarse horizon cut.
            |  horizon_buffer: Angle (float, in radians) of buffer for coarse horizon cut. Default is about 10 minutes of sky rotation.
            |                  (See caveats in simsetup.array_to_skymodel docstring)
            |  min_flux: Minimum stokes I flux to select [Jy]
            |  max_flux: Maximum stokes I flux to select [Jy]

    Returns:
        pyuvsim.SkyModel object
    """
    with open(catalog_csv, 'r') as cfile:
        header = cfile.readline()
    header = [h.strip() for h in header.split() if not h[0] == '[']  # Ignore units in header
    dt = np.format_parser(['U10', 'f8', 'f8', 'f8', 'f8'],
                          ['source_id', 'ra_j2000', 'dec_j2000', 'flux_density_I', 'frequency'], header)

    catalog_table = np.genfromtxt(catalog_csv, autostrip=True, skip_header=1,
                                  dtype=dt.dtype)

    catalog_table = np.atleast_1d(catalog_table)

    if len(source_select_kwds) > 0:
        catalog_table = source_cuts(catalog_table, input_uv=input_uv, **source_select_kwds)

    if return_table:
        return catalog_table

    return array_to_skymodel(catalog_table)


def write_catalog_to_file(filename, catalog):
    """
    Writes out a catalog to a text file, readable with simsetup.read_catalog_text()

    Args:
        filename: Path to output file (string)
        catalog: pyuvsim.SkyModel object
    """
    with open(filename, 'w+') as fo:
        fo.write("SOURCE_ID\tRA_J2000 [deg]\tDec_J2000 [deg]\tFlux [Jy]\tFrequency [Hz]\n")
        arr = skymodel_to_array(catalog)
        for src in arr:
            fo.write("{}\t{:f}\t{:f}\t{:0.2f}\t{:0.2f}\n".format(*src))


def create_mock_catalog(time, arrangement='zenith', array_location=None, Nsrcs=None,
                        alt=None, save=False, min_alt=None, rseed=None, return_table=False):
    """
    Create a mock catalog.

    SkyModel are defined in an AltAz frame at the given time, then returned in
    ICRS ra/dec coordinates.

    Args:
        time (float or astropy Time object): Julian date
        arrangement (str): Point source pattern (default = 1 source at zenith).
            Accepted arrangements:
                |  `triangle`:  Three point sources forming a triangle around the zenith
                |  `cross`: An asymmetric cross
                |  `zenith`: Some number of sources placed at the zenith.
                |  `off-zenith`:  A single source off zenith
                |  `long-line`:  Horizon to horizon line of point sources
                |  `hera_text`:  Spell out HERA around the zenith
                |  `random`:  Randomly distributed point sources near zenith
        Nsrcs (int):  Number of sources to put at zenith
        array_location (EarthLocation object): [Default = HERA site]
        alt (float): For off-zenith and triangle arrangements, altitude to place sources. (deg)
        min_alt (float): For random and long-line arrangements, minimum altitude at which to place sources. (deg)
        save (bool): Save mock catalog as npz file.
        rseed (int): If using the random configuration, pass in a RandomState seed.

    Returns:
        catalog: pyuvsim.SkyModel object (or a recarray of source parameters if return_table is True)
        mock_kwds: (dictionary) The keywords defining this source catalog
    """

    if not isinstance(time, Time):
        time = Time(time, scale='utc', format='jd')

    if array_location is None:
        array_location = EarthLocation(lat='-30d43m17.5s', lon='21d25m41.9s',
                                       height=1073.)
    freq = (150e6 * units.Hz)

    if arrangement not in ['off-zenith', 'zenith', 'cross', 'triangle', 'long-line', 'hera_text', 'random']:
        raise KeyError("Invalid mock catalog arrangement: " + str(arrangement))

    mock_keywords = {'time': time.jd, 'arrangement': arrangement,
                     'array_location': repr((array_location.lat.deg, array_location.lon.deg, array_location.height.value))}

    if arrangement == 'off-zenith':
        if alt is None:
            alt = 85.0  # Degrees
        mock_keywords['alt'] = alt
        Nsrcs = 1
        alts = [alt]
        azs = [90.]   # 0 = North pole, 90. = East pole
        fluxes = [1.0]

    if arrangement == 'triangle':
        Nsrcs = 3
        if alt is None:
            alt = 87.0  # Degrees
        mock_keywords['alt'] = alt
        alts = [alt, alt, alt]
        azs = [0., 120., 240.]
        fluxes = [1.0, 1.0, 1.0]

    if arrangement == 'cross':
        Nsrcs = 4
        alts = [88., 90., 86., 82.]
        azs = [270., 0., 90., 135.]
        fluxes = [5., 4., 1.0, 2.0]

    if arrangement == 'zenith':
        if Nsrcs is None:
            Nsrcs = 1
        mock_keywords['Nsrcs'] = Nsrcs
        alts = np.ones(Nsrcs) * 90.
        azs = np.zeros(Nsrcs, dtype=float)
        fluxes = np.ones(Nsrcs) * 1 / Nsrcs
        # Divide total Stokes I intensity among all sources
        # Test file has Stokes I = 1 Jy

    if arrangement == 'random':
        if Nsrcs is None:
            Nsrcs = 1
        if min_alt is None:
            min_alt = 30  # Degrees
        mock_keywords['Nsrcs'] = Nsrcs
        np.random.seed(seed=rseed)
        mock_keywords['rseed'] = np.random.get_state()[1][0]
        alts = np.random.uniform(min_alt, 90, Nsrcs)
        azs = np.random.uniform(0, 2 * np.pi, Nsrcs)
        fluxes = np.ones(Nsrcs, dtype=float)

    if arrangement == 'long-line':
        if Nsrcs is None:
            Nsrcs = 10
        if min_alt is None:
            min_alt = 5
        mock_keywords['Nsrcs'] = Nsrcs
        mock_keywords['alt'] = alt
        fluxes = np.ones(Nsrcs, dtype=float)
        if Nsrcs % 2 == 0:
            length = 180 - min_alt * 2
            spacing = length / (Nsrcs - 1)
            max_alt = 90. - spacing / 2
            alts = np.linspace(min_alt, max_alt, Nsrcs // 2)
            alts = np.append(alts, np.flip(alts, axis=0))
            azs = np.append(np.zeros(Nsrcs // 2, dtype=float) + 180.,
                            np.zeros(Nsrcs // 2, dtype=float))
        else:
            alts = np.linspace(min_alt, 90, (Nsrcs + 1) // 2)
            alts = np.append(alts, np.flip(alts[1:], axis=0))
            azs = np.append(np.zeros((Nsrcs + 1) // 2, dtype=float) + 180.,
                            np.zeros((Nsrcs - 1) // 2, dtype=float))

    if arrangement == 'hera_text':

        azs = np.array([-254.055, -248.199, -236.310, -225.000, -206.565,
                        -153.435, -123.690, -111.801, -105.945, -261.870,
                        -258.690, -251.565, -135.000, -116.565, -101.310,
                        -98.130, 90.000, 90.000, 90.000, 90.000, 90.000,
                        -90.000, -90.000, -90.000, -90.000, -90.000,
                        -90.000, 81.870, 78.690, 71.565, -45.000, -71.565,
                        -78.690, -81.870, 74.055, 68.199, 56.310, 45.000,
                        26.565, -26.565, -45.000, -56.310, -71.565])

        zas = np.array([7.280, 5.385, 3.606, 2.828, 2.236, 2.236, 3.606,
                        5.385, 7.280, 7.071, 5.099, 3.162, 1.414, 2.236,
                        5.099, 7.071, 7.000, 6.000, 5.000, 3.000, 2.000,
                        1.000, 2.000, 3.000, 5.000, 6.000, 7.000, 7.071,
                        5.099, 3.162, 1.414, 3.162, 5.099, 7.071, 7.280,
                        5.385, 3.606, 2.828, 2.236, 2.236, 2.828, 3.606, 6.325])

        alts = 90. - zas
        Nsrcs = alts.size
        fluxes = np.ones_like(alts)

    catalog = []

    source_coord = SkyCoord(alt=Angle(alts, unit=units.deg), az=Angle(azs, unit=units.deg),
                            obstime=time, frame='altaz', location=array_location)
    icrs_coord = source_coord.transform_to('icrs')

    ra = icrs_coord.ra
    dec = icrs_coord.dec
    names = np.array(['src' + str(si) for si in range(Nsrcs)])
    stokes = np.zeros((4, Nsrcs))
    stokes[0, :] = fluxes
    freqs = np.ones(Nsrcs) * freq
    catalog = SkyModel(names, ra, dec, freqs, stokes)
    if return_table:
        return skymodel_to_array(catalog), mock_keywords
    if get_rank() == 0 and save:
        np.savez('mock_catalog_' + arrangement, ra=ra.rad, dec=dec.rad, alts=alts, azs=azs, fluxes=fluxes)

    return catalog, mock_keywords


def initialize_catalog_from_params(obs_params, input_uv=None):
    """
    Make catalog from parameter file specifications.

    Default behavior is to do coarse horizon cuts.

    Args:
        obs_params: Either an obsparam file name or a dictionary of parameters.
        input_uv: (UVData object) Needed to know location and time for mock catalog
                  and for horizon cuts
    Returns:
        catalog: source recarray
        source_list_name: (str) Catalog identifier for metadata.
    """
    if input_uv is not None and not isinstance(input_uv, UVData):
        raise TypeError("input_uv must be UVData object")

    if isinstance(obs_params, str):
        with open(obs_params, 'r') as pfile:
            param_dict = yaml.safe_load(pfile)

        param_dict['config_path'] = os.path.dirname(obs_params)
    else:
        param_dict = obs_params

    # Parse source selection options
    select_options = ['min_flux', 'max_flux', 'horizon_buffer']
    source_select_kwds = {}

    source_params = param_dict['sources']
    if 'catalog' in source_params:
        catalog = source_params['catalog']
    else:
        raise KeyError("No catalog defined.")

    # Put catalog selection cuts in source section.
    for key in select_options:
        if key in source_params:
            source_select_kwds[key] = float(source_params[key])
    if catalog == 'mock':
        mock_keywords = {'arrangement': source_params['mock_arrangement']}
        extra_mock_kwds = ['time', 'Nsrcs', 'zen_ang', 'save', 'min_alt', 'array_location']
        for k in extra_mock_kwds:
            if k in source_params.keys():
                if k == 'array_location':
                    # String -- lat, lon, alt in degrees
                    latlonalt = [float(s.strip()) for s in source_params[k].split(',')]
                    lat, lon, alt = latlonalt
                    mock_keywords[k] = EarthLocation.from_geodetic(lon, lat, alt)
                else:
                    mock_keywords[k] = source_params[k]

        # time, arrangement, array_location, save, Nsrcs, min_alt

        if 'array_location' not in mock_keywords:
            if input_uv is not None:
                mock_keywords['array_location'] = EarthLocation.from_geocentric(*input_uv.telescope_location, unit='m')
            else:
                warnings.warn("No array_location specified. Defaulting to the HERA site.")
        if 'time' not in mock_keywords:
            if input_uv is not None:
                mock_keywords['time'] = input_uv.time_array[0]
                warnings.warn("Warning: No julian date given for mock catalog. Defaulting to first time step.")
            else:
                raise ValueError("input_uv must be supplied if using mock catalog without specified julian date")

        time = mock_keywords.pop('time')

        catalog, mock_keywords = create_mock_catalog(time, return_table=True, **mock_keywords)
        mock_keyvals = [str(key) + str(val) for key, val in six.iteritems(mock_keywords)]
        source_list_name = 'mock_' + "_".join(mock_keyvals)
    elif isinstance(catalog, str):
        source_list_name = os.path.basename(catalog)
        if not os.path.isfile(catalog):
            catalog = os.path.join(param_dict['config_path'], catalog)
        if catalog.endswith("txt"):
            catalog = read_text_catalog(catalog, return_table=True)
        elif catalog.endswith('vot'):
            catalog = read_votable_catalog(catalog, return_table=True)

    # Do source selections, if any.
    catalog = source_cuts(catalog, input_uv=input_uv, **source_select_kwds)

    return np.asarray(catalog), source_list_name


def parse_telescope_params(tele_params, config_path=''):
    """
    Parse the "telescope" section of obsparam.

    Args:
        tele_params: Dictionary of telescope parameters
            See pyuvsim documentation for allowable keys.
            https://pyuvsim.readthedocs.io/en/latest/parameter_files.html#telescope-configuration

        config_path: path to directory holding configuration and
            layout files.

    Returns:
        dict of array properties:
            |  Nants_data: Number of antennas
            |  Nants_telescope: Number of antennas
            |  antenna_names: list of antenna names
            |  antenna_numbers: corresponding list of antenna numbers
            |  antenna_positions: Array of ECEF antenna positions
            |  telescope_location: ECEF array center location
            |  telescope_location_lat_lon_alt: Lat Lon Alt array center location
            |  telescope_config_file: Path to configuration yaml file
            |  antenna_location_file: Path to csv layout file
            |  telescope_name: observatory name
        beam_list:  Beam models in the configuration.
        beam_dict:  Antenna numbers to beam indices
    """
    tele_params = copy.deepcopy(tele_params)
    # check for telescope config
    tele_config = 'telescope_config_name' in tele_params
    if tele_config:
        # parse telescope config
        if not os.path.isdir(config_path):
            config_path = os.path.dirname(config_path)
            if not os.path.isdir(config_path):
                raise ValueError('config_path {} is not a directory'.format(config_path))
        telescope_config_name = tele_params['telescope_config_name']
        if not os.path.exists(telescope_config_name):
            telescope_config_name = os.path.join(config_path, telescope_config_name)
            if not os.path.exists(telescope_config_name):
                raise ValueError('telescope_config_name file from yaml does not exist')
        with open(telescope_config_name, 'r') as yf:
            telconfig = yaml.safe_load(yf)
        telescope_location_latlonalt = ast.literal_eval(telconfig['telescope_location'])
        telescope_location = list(telescope_location_latlonalt)
        telescope_location[0] *= np.pi / 180.
        telescope_location[1] *= np.pi / 180.   # Convert to radians
        tele_params['telescope_location'] = uvutils.XYZ_from_LatLonAlt(*telescope_location)
        telescope_name = telconfig['telescope_name']

    else:
        # if not provided, get bare-minumum keys from tele_params
        if 'telescope_location' not in tele_params:
            raise KeyError("If telescope_config_name not provided in `telescope` obsparam section, you must provide telescope_location")
        if 'telescope_name' not in tele_params:
            raise KeyError("If telescope_config_name not provided in `telescope` obsparam section, you must provide telescope_name")
        telescope_location_latlonalt = tele_params['telescope_location']
        if isinstance(telescope_location_latlonalt, (str, np.str)):
            telescope_location_latlonalt = ast.literal_eval(telescope_location_latlonalt)
        telescope_location = list(telescope_location_latlonalt)
        telescope_location[0] *= np.pi / 180.
        telescope_location[1] *= np.pi / 180.   # Convert to radians
        telescope_name = tele_params['telescope_name']
        tele_params['telescope_location'] = uvutils.XYZ_from_LatLonAlt(*telescope_location)

    # get array layout
    if 'array_layout' not in tele_params:
        raise KeyError('array_layout must be provided.')
    array_layout = tele_params.pop('array_layout')
    if isinstance(array_layout, six.string_types):
        # Interpet as file path to layout csv file.
        layout_csv = array_layout
        # if array layout is a str, parse it as .csv filepath
        if isinstance(layout_csv, (str, np.str)):
            if not os.path.exists(layout_csv):
                layout_csv = os.path.join(config_path, layout_csv)
                if not os.path.exists(layout_csv):
                    raise ValueError('layout_csv file {} from yaml does not exist'.format(layout_csv))
            ant_layout = _parse_layout_csv(layout_csv)
            E, N, U = ant_layout['e'], ant_layout['n'], ant_layout['u']
            antnames = ant_layout['name']
            antnums = np.array(ant_layout['number'])
    elif isinstance(array_layout, dict):
        # Receiving antenna positions directly
        antnums = tele_params.pop('antenna_numbers')
        antnames = tele_params.pop('antenna_names')
        E, N, U = np.array([array_layout[an] for an in antnums]).T
        layout_csv = 'user-fed dict'

    # fill in outputs with just array info
    return_dict = {}
    beam_list = []
    beam_dict = {}

    return_dict['Nants_data'] = antnames.size
    return_dict['Nants_telescope'] = antnames.size
    return_dict['antenna_names'] = np.array(antnames.tolist())
    return_dict['antenna_numbers'] = np.array(antnums)
    antpos_enu = np.vstack((E, N, U)).T
    return_dict['antenna_positions'] = uvutils.ECEF_from_ENU(antpos_enu, *telescope_location) - tele_params['telescope_location']
    return_dict['array_layout'] = layout_csv
    return_dict['telescope_location'] = tuple(tele_params['telescope_location'])
    return_dict['telescope_location_lat_lon_alt'] = tuple(telescope_location_latlonalt)
    return_dict['telescope_name'] = telescope_name

    # if provided, parse sections related to beam files and types
    if not tele_config:
        return return_dict, beam_list, beam_dict

    return_dict['telescope_config_name'] = telescope_config_name
    beam_ids = ant_layout['beamid']
    beam_list = []
    beam_dict = {}

    for beamID in np.unique(beam_ids):
        beam_model = telconfig['beam_paths'][beamID]
        which_ants = antnames[np.where(beam_ids == beamID)]
        for a in which_ants:
            beam_dict[a] = beamID
        if beam_model in AnalyticBeam.supported_types:
            # Gaussian beam requires either diameter or sigma
            # Airy beam requires diameter
            if beam_model == 'uniform':
                beam_model = 'analytic_uniform'
            if beam_model == 'gaussian':
                try:
                    diam = telconfig['diameter']
                    beam_model = '_'.join(['analytic', beam_model, 'diam', str(diam)])
                except KeyError:
                    try:
                        sigma = telconfig['sigma']
                        beam_model = '_'.join(['analytic', beam_model, 'sig', str(sigma)])
                    except KeyError:
                        raise KeyError("Missing shape parameter for gaussian beam (diameter or sigma).")
            if beam_model == 'airy':
                try:
                    diam = telconfig['diameter']
                    beam_model = '_'.join(['analytic', beam_model, 'diam', str(diam)])
                except KeyError:
                    raise KeyError("Missing diameter for airy beam.")
        else:
            # If not analytic, it's a beamfits file path.
            if not os.path.exists(beam_model):
                path = os.path.join(SIM_DATA_PATH, beam_model)
                beam_model = path
                if not os.path.exists(path):
                    raise OSError("Could not find file " + beam_model)
        beam_list.append(beam_model)

    return return_dict, beam_list, beam_dict


def parse_frequency_params(freq_params):
    """
    Parse the "freq" section of obsparam.

    Args:
        freq_params: Dictionary of frequency parameters.
            See pyuvsim documentation for examples of allowable key combinations.
            https://pyuvsim.readthedocs.io/en/latest/parameter_files.html#frequency

    Returns:
        dict of array properties:
            |  channel_width: (float) Frequency channel spacing in Hz
            |  Nfreqs: (int) Number of frequencies
            |  freq_array: (dtype float, ndarray, shape=(Nspws, Nfreqs)) Frequency channel centers in Hz
    """
    freq_keywords = ['freq_array', 'start_freq', 'end_freq', 'Nfreqs',
                     'channel_width', 'bandwidth']
    fa, sf, ef, nf, cw, bw = [fk in freq_params for fk in freq_keywords]
    kws_used = ", ".join(sorted(freq_params.keys()))
    freq_params = copy.deepcopy(freq_params)
    init_freq_params = copy.deepcopy(freq_params)

    if fa:
        freq_arr = np.asarray(freq_params['freq_array'])
        freq_params['Nfreqs'] = freq_arr.size
        if freq_params['Nfreqs'] > 1:
            freq_params['channel_width'] = np.diff(freq_arr)[0]
            if not np.allclose(np.diff(freq_arr), freq_params['channel_width']):
                raise ValueError("Spacing in frequency array is uneven.")
        elif 'channel_width' not in freq_params:
            raise ValueError("Channel width must be specified "
                             "if freq_arr has length 1")
    else:
        if not (sf or ef):
            raise ValueError('Either start or end frequency must be specified: ' + kws_used)
        if not nf:
            if not cw:
                raise ValueError("Either channel_width or Nfreqs "
                                 " must be included in parameters:" + kws_used)
            if sf and ef:
                freq_params['bandwidth'] = freq_params['end_freq'] - freq_params['start_freq'] + freq_params['channel_width']
                bw = True
            if bw:
                Nfreqs = float(freq_params['bandwidth']
                               / freq_params['channel_width'])
                freq_params['Nfreqs'] = Nfreqs

            else:
                raise ValueError("Either bandwidth or band edges "
                                 "must be specified: " + kws_used)

        if not cw:
            if not bw:
                raise ValueError("Either bandwidth or channel width"
                                 " must be specified: " + kws_used)
            freq_params['channel_width'] = (freq_params['bandwidth']
                                            / float(freq_params['Nfreqs']))

        if not bw:
            freq_params['bandwidth'] = (freq_params['channel_width']
                                        * freq_params['Nfreqs'])
            bw = True

        if not sf:
            if ef and bw:
                freq_params['start_freq'] = freq_params['end_freq'] - freq_params['bandwidth'] + freq_params['channel_width']
        if not ef:
            if sf and bw:
                freq_params['end_freq'] = freq_params['start_freq'] + freq_params['bandwidth'] - freq_params['channel_width']

        if not np.isclose(freq_params['Nfreqs'] % 1, 0):
            raise ValueError("end_freq - start_freq must be evenly divisible by channel_width")
        freq_params['Nfreqs'] = int(freq_params['Nfreqs'])

        freq_arr = np.linspace(freq_params['start_freq'],
                               freq_params['end_freq'] + freq_params['channel_width'],
                               freq_params['Nfreqs'], endpoint=False)

    if freq_params['Nfreqs'] != 1:
        if not np.allclose(np.diff(freq_arr), freq_params['channel_width'] * np.ones(freq_params["Nfreqs"] - 1)):
            raise ValueError("Frequency array spacings are not equal to channel width."
                             + "\nInput parameters are: {}".format(str(init_freq_params)))

    Nspws = 1 if 'Nspws' not in freq_params else freq_params['Nspws']
    freq_arr = np.repeat(freq_arr, Nspws).reshape(Nspws, freq_params['Nfreqs'])

    return_dict = {}
    return_dict['Nfreqs'] = freq_params['Nfreqs']
    return_dict['freq_array'] = freq_arr
    return_dict['channel_width'] = freq_params['channel_width']
    return_dict['Nspws'] = 1

    return return_dict


def parse_time_params(time_params):
    """
    Parse the "time" section of obsparam.

    Args:
        time_params: Dictionary of time parameters
            See pyuvsim documentation for examples of allowable key combinations.
            https://pyuvsim.readthedocs.io/en/latest/parameter_files.html#time

    Returns:
        dict of array properties:
            |  integration_time: (float) Time array spacing in seconds.
            |  Ntimes: (int) Number of times
            |  start_time: (float) Starting time in Julian Date
            |  time_array: (dtype float, ndarray, shape=(Ntimes,)) Time step centers in JD.
    """
    return_dict = {}

    init_time_params = copy.deepcopy(time_params)
    time_params = copy.deepcopy(time_params)

    time_keywords = ['time_array', 'start_time', 'end_time', 'Ntimes', 'integration_time',
                     'duration_hours', 'duration_days']
    ta, st, et, nt, it, dh, dd = [tk in time_params for tk in time_keywords]
    kws_used = ", ".join(sorted(time_params.keys()))
    daysperhour = 1 / 24.
    hourspersec = 1 / 60.**2
    dayspersec = daysperhour * hourspersec

    if ta:
        # Time array is defined. Supersedes all other parameters:
        time_arr = time_params['time_array']
        time_params['Ntimes'] = len(time_arr)
        time_params['start_time'] = np.min(time_arr)

    else:
        if not (st or et):
            raise ValueError("Start or end time must be specified: " + kws_used)
        if dh and not dd:
            time_params['duration'] = time_params['duration_hours'] * daysperhour
            dd = True
        elif dd:
            time_params['duration'] = time_params['duration_days']

        if not nt:
            if not it:
                raise ValueError("Either integration_time or Ntimes must be "
                                 "included in parameters: " + kws_used)
            if st and et:
                time_params['duration'] = time_params['end_time'] - time_params['start_time'] + time_params['integration_time'] * dayspersec
                dd = True
            if dd:
                time_params['Ntimes'] = int(np.round(time_params['duration']
                                                     / (time_params['integration_time'] * dayspersec)))
            else:
                raise ValueError("Either duration or time bounds must be specified: "
                                 + kws_used)

        if not it:
            if not dd:
                raise ValueError("Either duration or integration time "
                                 "must be specified: " + kws_used)
            time_params['integration_time'] = (time_params['duration'] / dayspersec
                                               / float(time_params['Ntimes']))  # In seconds

        inttime_days = time_params['integration_time'] * dayspersec
        if not dd:
            time_params['duration'] = inttime_days * (time_params['Ntimes'])
            dd = True
        if not st:
            if et and dd:
                time_params['start_time'] = time_params['end_time'] - time_params['duration'] + inttime_days
        if not et:
            if st and dd:
                time_params['end_time'] = time_params['start_time'] + time_params['duration'] - inttime_days

        time_arr = np.linspace(time_params['start_time'],
                               time_params['end_time'] + inttime_days,
                               time_params['Ntimes'], endpoint=False)

        if time_params['Ntimes'] != 1:
            if not np.allclose(np.diff(time_arr), inttime_days * np.ones(time_params["Ntimes"] - 1), atol=dayspersec):   # To nearest second
                raise ValueError("Calculated time array is not consistent with set integration_time."
                                 + "\nInput parameters are: {}".format(str(init_time_params)))

        return_dict['integration_time'] = (np.ones_like(time_arr, dtype=np.float64)
                                           * time_params['integration_time'])
    return_dict['time_array'] = time_arr
    return_dict['Ntimes'] = time_params['Ntimes']
    return_dict['start_time'] = time_params['start_time']

    return return_dict


def freq_array_to_params(freq_array):
    """
    Give the channel width, bandwidth, start, and end frequencies corresponding
    to a given frequency array.

    Args:
        freq_array : (ndarray, shape = (Nfreqs,)) of frequencies [Hz].

    Returns:
        Dictionary of frequency parameters consistent with freq_array.
    """
    freq_array = np.asarray(freq_array).ravel()

    fdict = {}
    if freq_array.size < 2:
        fdict['channel_width'] = 1.0
        fdict['Nfreqs'] = 1
        fdict['start_freq'] = freq_array.item(0)
        return fdict

    fdict['channel_width'] = np.diff(freq_array).item(0)
    fdict['Nfreqs'] = freq_array.size
    fdict['bandwidth'] = fdict['channel_width'] * fdict['Nfreqs']
    fdict['start_freq'] = freq_array.item(0)
    fdict['end_freq'] = freq_array.item(-1)

    return fdict


def time_array_to_params(time_array):
    """
    Returns integration_time, duration, and start and end times corresponding to a given time array.

    Args:
        time_array : (ndarray) of julian dates

    Returns:
        Dictionary of time parameters consistent with time_array.
    """
    time_array = np.asarray(time_array)
    Ntimes_uniq = np.unique(time_array).size

    tdict = {}
    if Ntimes_uniq < 2:
        tdict['integration_time'] = 1.0
        tdict['Ntimes'] = 1
        tdict['start_time'] = time_array.item(0)
        return tdict

    if time_array.size > 1:
        dt = np.diff(np.unique(time_array))[0]
    else:
        dt = 1.0 / (24. * 3600.)
    if not np.allclose(np.diff(time_array), np.ones(time_array.size - 1) * dt):
        tdict['time_array'] = time_array.tolist()

    tdict['integration_time'] = np.min(np.diff(time_array)).item() * (24. * 3600.)
    tdict['Ntimes'] = Ntimes_uniq
    tdict['duration_days'] = tdict['integration_time'] * tdict['Ntimes'] / (24. * 3600.)
    tdict['start_time'] = time_array.item(0)
    tdict['end_time'] = time_array.item(-1)

    return tdict


def initialize_uvdata_from_params(obs_params):
    """
    Construct a uvdata object from parameters in a valid yaml file.

    Sufficient information must be provided by the parameters to define time and frequency arrays
    and verify the channel widths and time steps. This will error if insufficient or incompatible
    parameters are defined.

    The parameter dictionary may contain any valid UVData attributes as well.

    If the polarization array is not specified, it defaults to (XX, XY, YX, YY).

    Args:
        obs_params: Either an obs_param file name or a dictionary of parameters read in.
                    Any uvdata parameters may be passed in through here.
    Returns:
        uv_obj, beam_list, beam_dict
    """
    uvparam_dict = {}
    if isinstance(obs_params, str):
        param_dict = _config_str_to_dict(obs_params)
    else:
        param_dict = copy.deepcopy(obs_params)

    # Parse telescope parameters
    tele_dict = param_dict['telescope']
    uvparam_dict.update(tele_dict)
    tele_params, beam_list, beam_dict = parse_telescope_params(tele_dict, config_path=param_dict['config_path'])
    uvparam_dict.update(tele_params)

    # Use extra_keywords to pass along required paths for file history.
    extra_keywords = {}
    extra_keywords['param_file'] = param_dict['param_file']
    for key in ['telescope_config_name', 'array_layout']:
        if key in tele_dict:
            val = tele_dict[key]
            if isinstance(val, six.string_types):
                extra_keywords[key] = val

    uvparam_dict['extra_keywords'] = extra_keywords

    # Parse frequency structure
    freq_dict = param_dict['freq']
    uvparam_dict.update(parse_frequency_params(freq_dict))

    # Parse time structure
    time_dict = param_dict['time']
    uvparam_dict.update(parse_time_params(time_dict))

    # Parse polarizations
    if 'polarization_array' not in uvparam_dict:
        uvparam_dict['polarization_array'] = np.array([-5, -6, -7, -8])
    if 'Npols' not in uvparam_dict:
        uvparam_dict['Npols'] = len(uvparam_dict['polarization_array'])

    if 'object_name' not in param_dict:
        tloc = EarthLocation.from_geocentric(*uvparam_dict['telescope_location'], unit='m')
        time = Time(uvparam_dict['time_array'][0], scale='utc', format='jd')
        src, _ = create_mock_catalog(time, arrangement='zenith', array_location=tloc)
        src = src[0]
        if 'sources' in param_dict:
            source_file_name = os.path.basename(param_dict['sources']['catalog'])
            uvparam_dict['object_name'] = '{}_ra{:.4f}_dec{:.4f}'.format(source_file_name, src.ra.deg[0], src.dec.deg[0])
        else:
            uvparam_dict['object_name'] = 'Unspecified'
    else:
        uvparam_dict['object_name'] = param_dict['object_name']

    uv_obj = UVData()
    # use the __iter__ function on UVData to get list of UVParameters on UVData
    valid_param_names = [getattr(uv_obj, param).name for param in uv_obj]
    for k in valid_param_names:
        if k in param_dict:
            setattr(uv_obj, k, param_dict[k])
        if k in uvparam_dict:
            setattr(uv_obj, k, uvparam_dict[k])

    bls = np.array([uv_obj.antnums_to_baseline(uv_obj.antenna_numbers[j], uv_obj.antenna_numbers[i])
                    for i in range(0, uv_obj.Nants_data)
                    for j in range(i, uv_obj.Nants_data)])

    uv_obj.baseline_array = np.tile(bls, uv_obj.Ntimes)
    uv_obj.Nbls = bls.size
    uv_obj.time_array = np.repeat(uv_obj.time_array, uv_obj.Nbls)
    uv_obj.integration_time = np.repeat(uv_obj.integration_time, uv_obj.Nbls * uv_obj.Ntimes)
    uv_obj.Nblts = uv_obj.Nbls * uv_obj.Ntimes

    uv_obj.ant_1_array, uv_obj.ant_2_array = \
        uv_obj.baseline_to_antnums(uv_obj.baseline_array)

    # add other required metadata to allow select to work without errors
    # these will all be overwritten in uvsim.init_uvdata_out, so it's ok to hardcode them here
    uv_obj.set_lsts_from_time_array()
    uv_obj.set_uvws_from_antenna_positions()
    uv_obj.history = ''

    # select on object
    valid_select_keys = ['antenna_nums', 'antenna_names', 'ant_str', 'bls', 'frequencies', 'freq_chans', 'times', 'polarizations', 'blt_inds']

    # downselect baselines (or anything that can be passed to pyuvdata's select method)
    # Note: polarization selection is allowed here, but will cause an error if the incorrect pols are passed to pyuvsim.
    if 'select' in param_dict:
        select_params = param_dict['select']
        no_autos = bool(select_params.pop('no_autos', False))
        select_params = dict([(k, v) for k, v in six.iteritems(select_params) if k in valid_select_keys])
        if 'antenna_nums' in select_params:
            select_params['antenna_nums'] = list(map(int, select_params['antenna_nums']))
        redundant_threshold = param_dict['select'].get('redundant_threshold', None)
        if 'bls' in select_params:
            bls = select_params['bls']
            if isinstance(bls, six.string_types):
                # If read from file, this should be a string.
                bls = ast.literal_eval(bls)
                select_params['bls'] = bls
        if len(select_params) > 0:
            select_params['metadata_only'] = True
            uv_obj.select(**select_params)

        if no_autos:
            uv_obj.select(ant_str='cross', metadata_only=True)

        if redundant_threshold is not None:
            uv_obj.compress_by_redundancy(tol=redundant_threshold, metadata_only=True)

    return uv_obj, beam_list, beam_dict


def initialize_uvdata_from_keywords(yaml_filename=None, antenna_layout_filename=None, array_layout=None, telescope_location=None,
                                    telescope_name=None, Nfreqs=None, start_freq=None, bandwidth=None, freq_array=None, channel_width=None,
                                    Ntimes=None, integration_time=None, start_time=None, time_array=None,
                                    bls=None, antenna_nums=None, antenna_names=None, polarizations=None, no_autos=False,
                                    redundant_threshold=None, run_check=True, write_files=True, path_out=None, **kwargs):
    """
    Setup a UVData object from keywords.

    Args:
        yaml_filename : str (optional)
            Specify filename for yaml file to write out.
            Defaults to obsparam.yaml
        antenna_layout_filename : str (optional)
            Path to csv file of antenna positions (see documentation for details).
            Will not overwrite existing files.
            Defaults to antenna_layout.csv
        array_layout : dictionary (required if antenna_layout_filename not given)
            dictionary, keys are integer antenna numbers, values are len-3 antenna positions
            in ENU coordinates [meters].
        antenna_names : list of str (optional)
            If unset, antenna names are assigned as "%s" % antnum.
        telescope_location : len-3 tuple
            Telescope location on Earth in LatLonAlt coordinates [deg, deg, meters]
        telescope_name : str
            Name of telescope
        Nfreqs : int
            Number of frequency channels
        start_freq : float
            Starting frequency [Hz]
        bandwidth : float
            Total frequency bandwidth of spectral window [Hz]
        channel_width: float
            Frequency channel spacing [Hz]
        freq_array : ndarray
            frequency array [Hz], if this is specified, it supersedes Nfreqs, start_freq, bandwidth
        Ntimes : int
            Number of integration bins
        integration_time : float
            Width of time bins [seconds]
        start_time : float
            Time of the first integration bin [Julian Date]
        time_array : ndarray
            time array [Julian Date]. If this is specified it supersedes values of Ntimes, start_time and integration_time
        bls : list
            List of antenna-pair tuples for baseline selection
        redundant_threshold: float
            Redundant baseline selection tolerance for selection [meters]
        antenna_nums : list
            List of antenna numbers to keep in array
        polarizations : list
            List of polarization strings to insert into object
        no_autos : bool
            If True, eliminate all auto correlations
        write_files : bool
            If True, write out the parameter information to yaml files.
        path_out : str (optional)
            Path in which to place generated configuration files, if write_files is True.
            Defaults to current directory.
        kwargs : dictionary
            Any additional valid UVData attribute to assign to object.

    Returns:
        UVData object with zeroed data_array
    """

    if path_out is None:
        path_out = '.'
    if yaml_filename is None:
        yaml_filename = 'obsparam.yaml'
    if antenna_layout_filename is None:
        antenna_layout_filename = 'antenna_layout.csv'

    yaml_filename = check_file_exists_and_increment(os.path.join(path_out, yaml_filename))
    antenna_layout_filename = check_file_exists_and_increment(os.path.join(path_out, antenna_layout_filename))

    antenna_numbers = None
    if isinstance(array_layout, dict):
        antenna_numbers = np.fromiter(six.viewkeys(array_layout), dtype=int)
        antpos_enu = array_layout.values()
        if antenna_names is None:
            antenna_names = antenna_numbers.astype('str')
        if write_files:
            _write_layout_csv(antenna_layout_filename, antpos_enu, antenna_names, antenna_numbers)

    freq_params = {'Nfreqs': Nfreqs, 'start_freq': start_freq, 'bandwidth': bandwidth, 'freq_array': freq_array, 'channel_width': channel_width}
    time_params = {'Ntimes': Ntimes, 'start_time': start_time, 'integration_time': integration_time, 'time_array': time_array}
    selection_params = {'bls': bls, 'redundant_threshold': redundant_threshold, 'antenna_nums': antenna_nums, 'polarizations': polarizations, 'no_autos': no_autos}
    tele_params = {'telescope_location': repr(telescope_location), 'telescope_name': telescope_name}
    layout_params = {'antenna_names': antenna_names, 'antenna_numbers': antenna_numbers, 'array_layout': array_layout}

    freq_params = {k: v for k, v in six.iteritems(freq_params) if v is not None}
    time_params = {k: v for k, v in six.iteritems(time_params) if v is not None}
    selection_params = {k: v for k, v in six.iteritems(selection_params) if v is not None}
    tele_params = {k: v for k, v in six.iteritems(tele_params) if v is not None}
    layout_params = {k: v for k, v in six.iteritems(layout_params) if v is not None}

    uv_obj = UVData()

    valid_param_names = [getattr(uv_obj, param).name for param in uv_obj]

    extra_kwds = {k: v for k, v in six.iteritems(kwargs) if k in valid_param_names}

    param_dict = {
        'time': time_params,
        'freq': freq_params,
        'select': selection_params,
        'telescope': tele_params
    }

    param_dict['config_path'] = path_out
    param_dict['param_file'] = os.path.basename(yaml_filename)

    if write_files:
        tele_params['array_layout'] = antenna_layout_filename
        with open(yaml_filename, 'w') as yfile:
            yaml.dump(param_dict, yfile, default_flow_style=False)

    param_dict['telescope'].update(layout_params)
    uv_obj = initialize_uvdata_from_params(param_dict)

    return uv_obj


def uvdata_to_telescope_config(uvdata_in, beam_filepath, layout_csv_name=None,
                               telescope_config_name=None,
                               return_names=False, path_out='.'):
    """
    From a uvfits file, generate telescope parameters files.

    Output config files are written to the current directory, unless path_out is set.

    Args:
        uvdata_in (UVData): object to process
        path_out (str): Target directory for the config file.
        beam_filepath (str): Path to a beamfits file.
        layout_csv_name (str, optional): The name for the antenna positions
            csv file (Default <telescope_name>_layout.csv)
        telescope_config_name (str, optional): The name for the telescope config file
            (Default telescope_config_<telescope_name>.yaml)
        return_names (bool, optional): Return the file names for loopback tests.

    Returns:
        if return_names, returns tuple (path, telescope_config_name, layout_csv_name)
    """

    if telescope_config_name is None:
        telescope_config_path = \
            check_file_exists_and_increment(os.path.join(path_out, 'telescope_config_'
                                                         + uvdata_in.telescope_name
                                                         + '.yaml'))
        telescope_config_name = os.path.basename(telescope_config_path)

    if layout_csv_name is None:
        layout_csv_path = check_file_exists_and_increment(os.path.join(path_out, uvdata_in.telescope_name + "_layout.csv"))
        layout_csv_name = os.path.basename(layout_csv_path)

    antpos_enu, antenna_numbers = uvdata_in.get_ENU_antpos()

    _write_layout_csv(os.path.join(path_out, layout_csv_name), antpos_enu, uvdata_in.antenna_names, uvdata_in.antenna_numbers)

    # Write the rest to a yaml file.
    yaml_dict = dict(
        telescope_name=uvdata_in.telescope_name,
        telescope_location=repr(uvdata_in.telescope_location_lat_lon_alt_degrees),
        Nants=uvdata_in.Nants_telescope,
        beam_paths={
            0: beam_filepath
        }
    )

    with open(os.path.join(path_out, telescope_config_name), 'w+') as yfile:
        yaml.dump(yaml_dict, yfile, default_flow_style=False)

    print('Path: {}, telescope_config: {}, layout: {}'.format(path_out, telescope_config_name,
                                                              layout_csv_name))

    if return_names:
        return path_out, telescope_config_name, layout_csv_name


def uvdata_to_config_file(uvdata_in, param_filename=None, telescope_config_name='',
                          layout_csv_name='', catalog='mock', path_out='.'):
    """
    Extract simulation configuration settings from uvfits.

    Args:
        uvdata_in (UVData): uvdata object.

    Keywords:
        param_filename (str, optional): output param file name, defaults to obsparam_#.yaml.
        telescope_config_name (str, optional): Name of yaml file file. Defaults to blank string.
        layout_csv_name (str, optional): Name of layout csv file. Defaults to blank string.
        catalog (str, optional): Path to catalog file, defaults to 'mock'.
        path_out (str, optional): Where to put config files.
    """

    if param_filename is None:
        param_filename = check_file_exists_and_increment(os.path.join(path_out, 'obsparam.yaml'))
        param_filename = os.path.basename(param_filename)

    freq_array = uvdata_in.freq_array[0, :]
    time_array = uvdata_in.time_array
    integration_time_array = np.array(uvdata_in.integration_time)
    if np.max(integration_time_array) != np.min(integration_time_array):
        warnings.warn('The integration time is not constant. Using the shortest integration time')
    integration_time = float(np.min(integration_time_array))

    tdict = time_array_to_params(time_array)
    fdict = freq_array_to_params(freq_array)

    param_dict = dict(
        time=tdict,
        freq=fdict,
        sources=dict(
            catalog=catalog
        ),

        telescope=dict(
            telescope_config_name=telescope_config_name,
            array_layout=layout_csv_name
        ),

        filing=dict(
            outdir='.',
            outfile_name='',
            outfile_prefix=''
        )
    )

    if catalog == 'mock':
        param_dict['sources']['mock_arrangement'] = 'zenith'

    with open(os.path.join(path_out, param_filename), 'w') as yfile:
        yaml.dump(param_dict, yfile, default_flow_style=False)


def beam_string_to_object(beam_model):
    """
        Make a beam object given an identifying string.
    """
    # Identify analytic beams
    if beam_model.startswith('analytic'):
        if beam_model.startswith('analytic_uniform'):
            return AnalyticBeam('uniform')

        _, model, par, val = beam_model.split('_')
        if par == 'sig':
            return AnalyticBeam(model, sigma=float(val))
        if par == 'diam':
            return AnalyticBeam(model, diameter=float(val))

    path = beam_model   # beam_model = path to beamfits
    uvb = UVBeam()
    uvb.read_beamfits(path)
    if uvb.freq_interp_kind is None:
        uvb.freq_interp_kind = 'linear'
    return uvb
