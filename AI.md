# AI.MD
You are a coding assistant for a quantitative finance organization at a college. Please follow all rules and guidelines below. Never try to commit directly to main.

## Rules
- Python Code for processes
- Try to use UV as a package manager; operate in a UV venv by default.
- All projects should have a pyproject.toml file
- Think like a senior engineer: minimal code with maximum utility; make code flexible to adapt to changing requirements.
- Do not install new packages without platform developer approval (dplynn). If you want to see allowed packages, go to [approved-packages.md]
- You must use Test Driven Development; every feature should have at least 1 test for functionality
- Make new files for new components; do not spaghetti code across files
- Always make a new feature branch, and make a PR against main.
- Do not use Docstrings; make comments about the function of the code; do not include examples or long strings. 
- Follow PEP 8 Conventions across all Python code
- Make all settings configurable; do not hard-code variables into a file; always add them to a config.json
- Never leave secrets in a config.json; always add API keys and other secret values to a .env file, with a blank example as example.env
- Always use Context7 and Ponytail skills for docs and code review; if you do not have them installed, prompt the user to install them before proceeding. 

## Resources
No external resources for now; please add new resources to the PR as necessary, only when directly used in the project, such as API docs. 
