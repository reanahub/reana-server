.. include:: ../README.rst
   :end-before: About

.. include:: ../README.rst
   :start-after: =====
   :end-before: Features

Features:

.. include:: ../README.rst
   :start-line: 32
   :end-before: Useful links


CLI API
=======

.. click:: reana_server.reana_admin:reana_admin
   :prog: flask reana-admin
   :show-nested:


REST API
========

The REANA Server offers a REST API for management workloads
(workflows, jobs, tasks, etc.) running on REANA Cloud.
Detailed REST API documentation can be found `here <_static/api.html>`_.

.. automodule:: reana_server.rest.ping
   :members:

.. automodule:: reana_server.rest.users
   :members:

.. automodule:: reana_server.rest.workflows
   :members:

.. include:: ../CHANGES.rst

.. include:: ../CONTRIBUTING.rst

License
=======

.. include:: ../LICENSE

In applying this license, CERN does not waive the privileges and immunities
granted to it by virtue of its status as an Intergovernmental Organization or
submit itself to any jurisdiction.

.. include:: ../AUTHORS.rst
