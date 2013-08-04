import sys
import os

## from path import path

# PROJECT_ROOT = path(__file__).abspath().dirname().dirname()  # /mitx/lms
PROJECT_ROOT = os.path(__file__)
REPO_ROOT = PROJECT_ROOT.dirname()
COMMON_ROOT = REPO_ROOT / "common"
ENV_ROOT = REPO_ROOT.dirname()
print ENV_ROOT