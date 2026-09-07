import os

# Rich reads the terminal width once, when a Console is created at import time. Pin it before
# anything imports the CLI so tables and help render the same on every machine and in CI.
os.environ["COLUMNS"] = "200"
