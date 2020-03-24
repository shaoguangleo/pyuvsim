# -*- mode: python; coding: utf-8 -*
# Copyright (c) 2018 Radio Astronomy Software Group
# Licensed under the 3-clause BSD License

import glob
import io

from setuptools import setup


def branch_scheme(version):
    """Local version scheme that adds the branch name for absolute reproducibility."""
    if version.exact or version.node is None:
        return version.format_choice("", "+d{time:{time_format}}", time_format="%Y%m%d")
    else:
        if version.branch == "master":
            return version.format_choice("+{node}", "+{node}.dirty")
        else:
            return version.format_choice("+{node}.{branch}", "+{node}.{branch}.dirty")


with io.open('README.md', 'r', encoding='utf-8') as readme_file:
    readme = readme_file.read()

setup_args = {
    'name': 'pyuvsim',
    'author': 'Radio Astronomy Software Group',
    'url': 'https://github.com/RadioAstronomySoftwareGroup/pyuvsim',
    'license': 'BSD',
    'description': 'A comprehensive simulation package for radio interferometers in python',
    'long_description': readme,
    'long_description_content_type': 'text/markdown',
    'package_dir': {'pyuvsim': 'pyuvsim'},
    'packages': ['pyuvsim', 'pyuvsim.tests'],
    'scripts': glob.glob('scripts/*'),
    'use_scm_version': {'local_scheme': branch_scheme},
    'include_package_data': True,
    'install_requires': ['numpy>=1.15', 'scipy', 'astropy>=4.0', 'pyyaml', 'pyuvdata'],
    'test_requires': ['pytest'],
    'classifiers': ['Development Status :: 5 - Production/Stable',
                    'Intended Audience :: Science/Research',
                    'License :: OSI Approved :: BSD License',
                    'Programming Language :: Python :: 3.6',
                    'Topic :: Scientific/Engineering :: Astronomy'],
    'keywords': 'radio astronomy interferometry',
    'extras_require': {
        'sim': ['mpi4py>=3.0.0', 'psutil'],
        'all': ['mpi4py>=3.0.0', 'psutil', 'line_profiler'],
        'dev': ['mpi4py>=3.0.0', 'psutil', 'line_profiler', 'pypandoc',
                'pytest', 'pytest-cov', 'sphinx', 'pre-commit']
    }
}

if __name__ == '__main__':
    setup(**setup_args)
