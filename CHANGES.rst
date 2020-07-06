Changes
=======

Version master (UNRELEASED)
---------------------------

- Pins all Python dependencies allowing to easily rebuild component images at later times.
- Adds new endpoint to request user tokens.
- Adds email notifications on relevant events such as user token granted/revoked.
- Adds possibility to query logs for a single workflow step.
- Adds endpoint to retrieve the workflow specification used for the workflow run.
- Adds preview flag to download file endpoint.
- Validates submitted operational options before starting a workflow.
- Includes workflow progress in the workflow list endpoint response payload.
- Allows uploading empty files.
- Adds new block size which specifies the type of units to use for disk size.
- Fixes bug related to rescheduling deleted workflows.
- Workflow restart accepts now new workflow specifications.
- Adds new command to generate a status report for the REANA system (useful as cronjob).
- Adds user token management commands (grant/revoke).
- Extends import/export commands with new user model fields.
- Installs submodels in editable mode for live code updates.
- Adds Black formatter support.
- Bumps to Invenio-Accounts 1.3.0 to support REST API.
- Adds support to local users.
- Renames ``/api/me`` to ``/api/you`` due to conflict with Invenio-Accounts.

Version 0.6.1 (2020-05-25)
--------------------------

- Upgrades REANA-Commons package using latest Kubernetes Python client version.
- Pins Flask and Invenio dependencies to fix REANA 0.6 installation troubles.

Version 0.6.0 (2019-12-20)
--------------------------

- Fixes bug with big file uploads by using data streaming.
- Adds user login endpoints using OAuth, currently configured to work with CERN
  SSO but extensible to use other OAuth providers such as GitHub, more in `Invenio-OAuthClient <https://invenio-oauthclient.readthedocs.io/en/latest/>`_.
- Adds endpoints to integrate with GitLab (for retrieving user projects and creating/deleting webhooks).
- Adds new endpoint ``/me`` to retrieve user information.
- Improves security by allowing requests only with ``REANA_URL`` in the host header, avoiding host header injection attacks.
- Initialisation logs moved from ``stdout`` to ``/var/log/reana-server-init-output.log``.

Version 0.5.0 (2019-04-23)
--------------------------

- Adds new endpoint to compare two workflows. The output is a ``git`` like
  diff which can be configured to show differences at metadata level,
  workspace level or both.
- Adds new endpoint to retrieve workflow parameters.
- Adds new endpoint to query the disk usage of a given workspace.
- Adds new endpoints to delete and move files whithin the workspace.
- Adds new endpoints to open and close interactive sessions inside the
  workspace.
- Workflow start does not send start requests to REANA Workflow Controller
  straight away, instead it will decide whether REANA can execute it or queue
  it depending on a set of conditions, currently it depends on the number of
  running jobs in the cluster.
- Adds new administrator command to export and import all REANA users.

Version 0.4.0 (2018-11-06)
--------------------------

- Improves REST API documentation rendering.
- Enhances test suite and increases code coverage.
- Changes license to MIT.

Version 0.3.1 (2018-09-07)
--------------------------

- Harmonises date and time outputs amongst various REST API endpoints.
- Pins REANA-Commons, REANA-DB and Bravado dependencies.

Version 0.3.0 (2018-08-10)
--------------------------

- Adds support of Serial workflows.
- Adds API protection with API tokens.

Version 0.2.0 (2018-04-19)
--------------------------

- Adds support of Common Workflow Language workflows.
- Adds support of specifying workflow names in REST API requests.
- Improves error messages and information.

Version 0.1.0 (2018-01-30)
--------------------------

- Initial public release.

.. admonition:: Please beware

   Please note that REANA is in an early alpha stage of its development. The
   developer preview releases are meant for early adopters and testers. Please
   don't rely on released versions for any production purposes yet.
