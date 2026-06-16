from .search import search
from .search_code import search_code
from .search_docs import search_docs
from .read_file import read_file
from .grep import grep
from .web_search import web_search
from .web_fetch import web_fetch
from .find_symbol import find_symbol
from .find_callers import find_callers
from .find_callees import find_callees
from .execute_code import execute_code
from .repo_info import repo_info
from .docs_mentioning import docs_mentioning
from .git_history import git_log, git_show, git_blame, find_commits_for_jira
from .jira import jira_lookup, jira_search

ALL_TOOLS = [
    search,
    search_code,
    search_docs,
    docs_mentioning,
    read_file,
    grep,
    find_symbol,
    find_callers,
    find_callees,
    repo_info,
    git_log,
    find_commits_for_jira,
    git_show,
    git_blame,
    jira_lookup,
    jira_search,
    execute_code,
    web_search,
    web_fetch,
]
TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}
