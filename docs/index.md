```{include} ../README.md
:end-before: "## About"
```

```{include} ../README.md
:start-after: "## About"
:end-before: "## Useful links"

```

## CLI API

```{eval-rst}
.. click:: reana_server.reana_admin:reana_admin
   :prog: flask reana-admin
   :show-nested:

```

## REST API

The REANA Server offers a REST API for management workloads
(workflows, jobs, tasks, etc.) running on REANA Cloud.
Detailed REST API documentation can be found <a href="_static/api.html">here</a>.

```{eval-rst}
.. automodule:: reana_server.rest.ping
   :members:
```

```{eval-rst}
.. automodule:: reana_server.rest.users
   :members:
```

```{eval-rst}
.. automodule:: reana_server.rest.workflows
   :members:
```

```{include} ../CHANGELOG.md
:heading-offset: 1
```

```{include} ../CONTRIBUTING.md
:heading-offset: 1
```

## License

```{eval-rst}
.. include:: ../LICENSE
```

In applying this license, CERN does not waive the privileges and immunities
granted to it by virtue of its status as an Intergovernmental Organization or
submit itself to any jurisdiction.

```{include} ../AUTHORS.md
:heading-offset: 1
```
