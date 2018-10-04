# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# REANA is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with REANA; if not, see <http://www.gnu.org/licenses>.
#
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization or
# submit itself to any jurisdiction.

"""REANA-Server."""

from __future__ import absolute_import, print_function

import os
import re

from setuptools import find_packages, setup

readme = open('README.rst').read()
history = open('CHANGES.rst').read()

tests_require = [
    'apispec>=0.21.0',
    'check-manifest>=0.25',
    'coverage>=4.0',
    'isort>=4.2.15',
    'marshmallow>=2.13',
    'pydocstyle>=1.0.0',
    'pytest-cache>=1.0',
    'pytest-cov>=1.8.0',
    'pytest-pep8>=1.0.6',
    'pytest>=2.8.0',
    'swagger_spec_validator>=2.1.0'
]

extras_require = {
    'docs': [
        'Sphinx>=1.4.4,<1.6',
        'sphinx-rtd-theme>=0.1.9',
        'sphinxcontrib-httpdomain>=1.5.0',
        'sphinxcontrib-openapi>=0.3.0'
    ],
    'tests': tests_require,
}

extras_require['all'] = []
for key, reqs in extras_require.items():
    if ':' == key[0]:
        continue
    extras_require['all'].extend(reqs)

setup_requires = [
    'pytest-runner>=2.7',
]

install_requires = [
    'click>=6.7',
    'Flask>=0.11',
    'fs>=2.0',
    'flask-cors>=3.0.6',
    'marshmallow>=2.13',
    'pyOpenSSL==17.3.0',  # FIXME remove once yadage-schemas solves deps.
    'reana-commons>=0.3.1,<0.4.0',
    'reana-pytest-commons',
    'reana-db>=0.3.0,<0.4.0',
    'requests==2.11.1',
    'rfc3987==1.3.7',  # FIXME remove once yadage-schemas solves deps.
    'strict-rfc3339==0.7',  # FIXME remove once yadage-schemas solves deps.
    'tablib>=0.12.1',
    'uWSGI>=2.0.17',
    'uwsgi-tools>=1.1.1',
    'uwsgitop>=0.10',
    'webcolors==1.7',  # FIXME remove once yadage-schemas solves deps.
]

packages = find_packages()


# Get the version string. Cannot be done with import!
with open(os.path.join('reana_server', 'version.py'), 'rt') as f:
    version = re.search(
        '__version__\s*=\s*"(?P<version>.*)"\n',
        f.read()
    ).group('version')

setup(
    name='reana-server',
    version=version,
    description=__doc__,
    long_description=readme + '\n\n' + history,
    author='REANA',
    author_email='info@reana.io',
    url='https://github.com/reanahub/reana-server',
    packages=['reana_server'],
    zip_safe=False,
    entry_points={
        'flask.commands': [
            'db = reana_server.cli:db',
            'users = reana_server.cli:users',
        ]
    },
    include_package_data=True,
    extras_require=extras_require,
    install_requires=install_requires,
    setup_requires=setup_requires,
    tests_require=tests_require,
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Environment :: Web Environment',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: GNU General Public License v2 (GPLv2)',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: Implementation :: CPython',
        'Programming Language :: Python',
        'Programming Language :: Python',
        'Topic :: Internet :: WWW/HTTP :: Dynamic Content',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],
)
