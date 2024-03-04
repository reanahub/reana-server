# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Server."""

from __future__ import absolute_import, print_function

import os
import re

from setuptools import find_packages, setup

readme = open("README.md").read()
history = open("CHANGELOG.md").read()

tests_require = [
    "pytest-reana>=0.9.2,<0.10.0",
]

extras_require = {
    "debug": [
        "wdb",
        "ipdb",
        "Flask-DebugToolbar",
    ],
    "docs": [
        "myst-parser",
        "Sphinx>=1.5.1",
        "sphinx-rtd-theme>=0.1.9",
        "sphinxcontrib-httpdomain>=1.5.0",
        "sphinxcontrib-openapi>=0.8.0",
        "sphinxcontrib-redoc>=1.5.1",
        "sphinx-click>=1.0.4",
    ],
    "tests": tests_require,
}

extras_require["all"] = []
for key, reqs in extras_require.items():
    if ":" == key[0]:
        continue
    extras_require["all"].extend(reqs)

setup_requires = [
    "pytest-runner>=2.7",
]

install_requires = [
    # These bounds on Flask/Werkzeug are needed because Werkzeug v2.1 removes `safe_str_cmp`,
    # which is needed by Flask-Login. This was fixed in Flask-Login v0.6.0 (see the issue
    # https://github.com/maxcountryman/flask-login/pull/585), but invenio-accounts<2
    # requires Flask-Login<0.5
    "Flask>=2.1.1,<2.2.0",
    "Werkzeug<2.1",
    "flask-celeryext<0.5.0",
    "gitpython>=3.1",
    "marshmallow>2.13.0,<=2.20.1",
    "reana-commons[kubernetes,yadage,snakemake,cwl]>=0.9.8,<0.10.0",
    "reana-db>=0.9.4,<0.10.0",
    "requests==2.25.0",
    "tablib>=0.12.1",
    "uWSGI>=2.0.17",
    "uwsgi-tools>=1.1.1",
    "uwsgitop>=0.10",
    "wtforms<3.0.0",
    # Yadage dependencies
    # Pinning adage/packtivity/yadage/yadage-schemas to make sure we use compatible versions.
    # See https://github.com/reanahub/reana-workflow-engine-yadage/pull/236#discussion_r992475484
    "adage==0.10.1",
    "packtivity==0.14.24",
    "yadage==0.20.1",
    "yadage-schemas==0.10.6",
    # Invenio dependencies
    "invenio-app>=1.3.0,<1.4.0",
    "invenio-base>=1.2.0,<1.3.0",
    "invenio-cache>=1.1.0,<1.2.0",
    "invenio-config>=1.0.3,<1.1.0",
    # From base bundle
    "invenio-logging>=1.3.0,<1.4.0",
    "invenio-mail>=1.0.2,<1.1.0",
    # From auth bundle
    "invenio-accounts>=1.4.2,<1.5.0",
    "invenio-oauth2server>=1.3.0,<1.4.0",
    "invenio-oauthclient>=1.5.0,<1.6.0",
    "invenio-userprofiles>=1.2.0,<1.3.0",
    "invenio-userprofiles>=1.2.0,<1.2.5",  # pin <1.2.5 due to import-related changes in upstream
    "invenio-theme>=1.4.7,<2.0.0",
    "invenio-i18n>=1.3.3,<2.0.0",
    # Invenio database
    "invenio-db[postgresql]>=1.0.5,<1.1.0",
    "six>=1.12.0",  # required by Flask-Breadcrumbs
]

packages = find_packages()


# Get the version string. Cannot be done with import!
with open(os.path.join("reana_server", "version.py"), "rt") as f:
    version = re.search(r'__version__\s*=\s*"(?P<version>.*)"\n', f.read()).group(
        "version"
    )

setup(
    name="reana-server",
    version=version,
    description=__doc__,
    long_description=readme + "\n\n" + history,
    long_description_content_type="text/markdown",
    author="REANA",
    author_email="info@reana.io",
    url="https://github.com/reanahub/reana-server",
    packages=["reana_server"],
    zip_safe=False,
    entry_points={
        "flask.commands": [
            "reana-admin = reana_server.reana_admin:reana_admin",
            "start-scheduler = reana_server.cli:start_scheduler",
        ],
        "invenio_base.apps": ["reana = reana_server.ext:REANA"],
        "invenio_base.api_apps": ["reana = reana_server.ext:REANA"],
        "invenio_config.module": [
            "reana_server = reana_server.config",
        ],
        "invenio_base.api_blueprints": [
            "reana_server_ping = reana_server.rest.ping:blueprint",
            "reana_server_workflows = reana_server.rest.workflows:blueprint",
            "reana_server_users = reana_server.rest.users:blueprint",
            "reana_server_secrets = reana_server.rest.secrets:blueprint",
            "reana_server_gitlab = reana_server.rest.gitlab:blueprint",
            "reana_server_config = reana_server.rest.config:blueprint",
            "reana_server_status = reana_server.rest.status:blueprint",
            "reana_server_info = reana_server.rest.info:blueprint",
            "reana_server_launch = reana_server.rest.launch:blueprint",
        ],
    },
    include_package_data=True,
    extras_require=extras_require,
    install_requires=install_requires,
    setup_requires=setup_requires,
    tests_require=tests_require,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Web Environment",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python",
        "Programming Language :: Python",
        "Topic :: Internet :: WWW/HTTP :: Dynamic Content",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
