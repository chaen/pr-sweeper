#!/usr/bin/env bash
set -e

if [ "${BRANCH}" == "auto" ]; then
  export BRANCH="upstream/$(sed 's@.*/@@' <<< $GITHUB_REF)"
fi

if [ -z "${GITHUB_PAT}" ]; then
  echo "You did not set the GitHub Personal Access Token variable github-pat!"
  exit 1
fi

if [ "${PROJECT_NAME}" == "auto" ]; then
  export PROJECT_NAME=${GITHUB_REPOSITORY}
fi

git config --global user.email "noreply@github.com"
git config --global user.name "sweeper"

if [ "$1" == "local" ]; then
  . sweep_PR.py \
    -b "${BRANCH}" \
    -p "${PROJECT_NAME}" \
    --pr-project-name "${PR_PROJECT_NAME:-${PROJECT_NAME}}" \
    -t "${GITHUB_PAT}" \
    --repository-root "${GITHUB_WORKSPACE}" \
    -s "${SINCE}" \
    -g "${STRATEGY}" \
    -u "${UNTIL}"
else
  $THIS/sweep_PR.py \
    -b "${BRANCH}" \
    -p "${PROJECT_NAME}" \
    --pr-project-name "${PR_PROJECT_NAME:-${PROJECT_NAME}}" \
    -t "${GITHUB_PAT}" \
    --repository-root "${GITHUB_WORKSPACE}" \
    -s "${SINCE}" \
    -g "${STRATEGY}" \
    -u "${UNTIL}"
fi
