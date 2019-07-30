# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server."""

from __future__ import absolute_import, print_function

import os
import re

from setuptools import find_packages, setup

readme = open('README.rst').read()
history = open('CHANGES.rst').read()

tests_require = [
    'pytest-reana>=0.6.0.dev20190705,<0.7.0',
]

extras_require = {
    'docs': [
        'Sphinx>=1.4.4,<1.6',
        'sphinx-rtd-theme>=0.1.9',
        'sphinxcontrib-httpdomain>=1.5.0',
        'sphinxcontrib-openapi>=0.3.0,<0.4.0',
        'sphinxcontrib-redoc>=1.5.1',
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
    'fs>=2.0',
    'marshmallow>=2.13',
    'pyOpenSSL==17.5.0',
    'reana-commons[kubernetes]>=0.6.0.dev20190812,<0.7.0',
    'reana-db>=0.6.0.dev20190715,<0.7.0',
    'requests==2.20.0',
    'rfc3987==1.3.7',
    'strict-rfc3339==0.7',
    'tablib>=0.12.1',
    'uWSGI>=2.0.17',
    'uwsgi-tools>=1.1.1',
    'uwsgitop>=0.10',
    'webcolors==1.7',
    # Invenio dependencies
    'Flask>=1.0.2',
    'invenio-app>=1.1.0,<1.2.0',
    'invenio-base>=1.0.2,<1.1.0',
    'invenio-cache>=1.0.0,<1.1.0',
    'invenio-config>=1.0.1,<1.1.0',
    # From base bundle
    'invenio-logging>=1.1.0,<1.2.0',
    'invenio-mail>=1.0.2,<1.1.0',
    'invenio-rest>=1.0.0,<1.1.0',
    # From auth bundle
    'invenio-accounts>=1.1.1',
    'invenio-oauth2server>=1.0.3,<1.1.0',
    'invenio-oauthclient>=1.1.2,<1.2.0',
    'invenio-userprofiles>=1.0.1,<1.1.0',
    # Not included in previous dependencies
    'invenio-db[postgresql]>=1.0.3,<1.1.0',
    # Invenio dependencies missing from Invenio packages setup.py
    'jsonpatch>=1.23,<2',
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
            'reana-db = reana_server.cli:reana_db',
            'reana-users = reana_server.cli:reana_users',
            'start-scheduler = reana_server.cli:start_scheduler',
        ],
        "invenio_base.apps": [
            "reana = reana_server.ext:REANA"
        ],
        'console_scripts': [
            'reana-server = invenio_app.cli:cli',
        ],
        'invenio_config.module': [
            'reana_server = reana_server.config',
        ],
        "invenio_base.api_blueprints": [
            "reana_server_ping = reana_server.rest.ping:blueprint",
            "reana_server_workflows = reana_server.rest.workflows:blueprint",
            "reana_server_users = reana_server.rest.users:blueprint",
            "reana_server_secrets = reana_server.rest.secrets:blueprint",
            "reana_server_gitlab = reana_server.rest.gitlab:blueprint",
        ],
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
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: Implementation :: CPython',
        'Programming Language :: Python',
        'Programming Language :: Python',
        'Topic :: Internet :: WWW/HTTP :: Dynamic Content',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],
)
