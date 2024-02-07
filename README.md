# REANA-Server

[![image](https://github.com/reanahub/reana-server/workflows/CI/badge.svg)](https://github.com/reanahub/reana-server/actions)
[![image](https://readthedocs.org/projects/reana-server/badge/?version=latest)](https://reana-server.readthedocs.io/en/latest/?badge=latest)
[![image](https://codecov.io/gh/reanahub/reana-server/branch/master/graph/badge.svg)](https://codecov.io/gh/reanahub/reana-server)
[![image](https://img.shields.io/badge/discourse-forum-blue.svg)](https://forum.reana.io)
[![image](https://img.shields.io/github/license/reanahub/reana-server.svg)](https://github.com/reanahub/reana-server/blob/master/LICENSE)
[![image](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

## About

REANA-Server is a component of the [REANA](http://www.reana.io/) reusable and
reproducible research data analysis platform. It implements the API Server that takes and
performs REST API calls issued by REANA clients.

## Features

- offers rich REST API services for REANA clients
- transmits REST API requests to appropriate REANA cloud components
- REST API to run research analysis workflows on compute clouds
- REST API to list submitted workflows and enquire about their statuses
- REST API to manage analysis files
- REST API to download results of finished analysis workflows
- REST API to find the differences between two workflows (`git` like output)

## Usage

The detailed information on how to install and use REANA can be found in
[docs.reana.io](https://docs.reana.io).

## Useful links

- [REANA project home page](http://www.reana.io/)
- [REANA user documentation](https://docs.reana.io)
- [REANA user support forum](https://forum.reana.io)
- [REANA-Server releases](https://reana-server.readthedocs.io/en/latest#changes)
- [REANA-Server docker images](https://hub.docker.com/r/reanahub/reana-server)
- [REANA-Server developer documentation](https://reana-server.readthedocs.io/)
- [REANA-Server known issues](https://github.com/reanahub/reana-server/issues)
- [REANA-Server source code](https://github.com/reanahub/reana-server)
