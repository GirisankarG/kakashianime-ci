# scheduled checks

A daily health check that runs from a private repository. This repository
holds only the workflow and a standard-library mail script.

The mail script lives here, not in the private checkout, on purpose: if that
checkout fails, the step that reports the failure must not depend on it.

Everything that identifies what is being checked arrives through repository
secrets at run time.
