APPSEC_ADVISOR_URL ?= https://github.com/appsec-foundry/appsec-advisor.git
# The initializer replaces this template default with either the resolved stable
# release tag or the moving dev branch. Treat an explicitly exported empty CI
# variable as unset so generated repositories still use their persisted choice.
APPSEC_ADVISOR_REF ?=
ifeq ($(strip $(APPSEC_ADVISOR_REF)),)
APPSEC_ADVISOR_REF := v0.6.0-beta.1
endif
APPSEC_ADVISOR_DEST ?= upstream/appsec-advisor
APPSEC_ADVISOR_SOURCE ?= $(APPSEC_ADVISOR_DEST)
INTERNAL_NAME ?= acme-appsec
# Internal packaging repository shown in packaged help and README output. Keep
# empty until the internal HTTPS URL exists.
INTERNAL_REPOSITORY_URL ?=
# Organization-owned version shown in the plugin banner, help, manifest and
# archive name. Bump it when publishing a new internal package release.
PACKAGE_VERSION ?= 0.1.0
# Backward-compatible one-off override; normally leave this empty and edit
# PACKAGE_VERSION instead.
VERSION ?=
RELEASE_VERSION ?=
export PACKAGE_VERSION VERSION INTERNAL_REPOSITORY_URL
LOCAL_MARKETPLACE_NAME ?= $(INTERNAL_NAME)-local
LOCAL_MARKETPLACE_SCOPE ?= local
APPSEC_ADVISOR_TEMPLATE_URL ?= https://github.com/appsec-foundry/appsec-advisor-packaging-template.git
# The initializer replaces this moving bootstrap ref with the exact template
# commit used to create or reinitialize a generated repository.
APPSEC_ADVISOR_TEMPLATE_REF ?= main
APPSEC_ADVISOR_TEMPLATE_SOURCE ?=
REINIT_BUILD ?= 1
export APPSEC_ADVISOR_TEMPLATE_URL APPSEC_ADVISOR_TEMPLATE_REF APPSEC_ADVISOR_TEMPLATE_SOURCE REINIT_BUILD

# Baseline source selected by the initializer: generic AISCB, a temporary Git
# fetch, one HTTPS document, or disabled. Organization sources own composition
# of the generic baseline and their overlay; packaging consumes reviewed copies.
BASELINE_SOURCE_KIND ?= aiscb
# Git and HTTPS organization modes persist a remote URL. Git also uses a ref
# and paths inside that ref. ORG_BASELINE_SOURCE is an optional existing local
# checkout override and remains the compatibility path for older scaffolds.
ORG_BASELINE_URL ?=
ORG_BASELINE_REF ?= main
ORG_BASELINE_SOURCE ?=
ORG_BASELINE_DOC ?=
ORG_BASELINE_SKILLS_DIR ?=

# Environment passed to the fetch and packaging scripts. The recipes are
# silenced with '@' so the scripts' own '==>' progress lines start the output
# instead of a 200-character variable assignment.
UPSTREAM_ENV := APPSEC_ADVISOR_URL="$(APPSEC_ADVISOR_URL)" APPSEC_ADVISOR_REF="$(APPSEC_ADVISOR_REF)" APPSEC_ADVISOR_DEST="$(APPSEC_ADVISOR_DEST)"
PACKAGE_ENV := $(UPSTREAM_ENV) APPSEC_ADVISOR_SOURCE="$(APPSEC_ADVISOR_SOURCE)" INTERNAL_NAME="$(INTERNAL_NAME)" BASELINE_SOURCE_KIND="$(BASELINE_SOURCE_KIND)"

# Printed by the read-only check targets when they exit 1, so the Make error
# that follows reads as the drift signal it is.
DRIFT_NOTE := NOTE: the finding above is the result — these checks report drift by failing, so the 'Error 1' below is that signal and not a broken build.

ifeq ($(APPSEC_ADVISOR_SOURCE),$(APPSEC_ADVISOR_DEST))
FETCH_TARGET := fetch-upstream
else
FETCH_TARGET :=
endif

ifeq ($(BASELINE_SOURCE_KIND),aiscb)
BASELINE_SYNC_TARGET := baseline-sync-aiscb
BASELINE_SYNC_CHECK_TARGET := baseline-sync-check-aiscb
else ifeq ($(BASELINE_SOURCE_KIND),organization)
BASELINE_SYNC_TARGET := baseline-sync-organization
BASELINE_SYNC_CHECK_TARGET := baseline-sync-check-organization
else ifeq ($(BASELINE_SOURCE_KIND),organization-git)
BASELINE_SYNC_TARGET := baseline-sync-organization-git
BASELINE_SYNC_CHECK_TARGET := baseline-sync-check-organization-git
else ifeq ($(BASELINE_SOURCE_KIND),organization-https)
BASELINE_SYNC_TARGET := baseline-sync-organization-https
BASELINE_SYNC_CHECK_TARGET := baseline-sync-check-organization-https
else ifeq ($(BASELINE_SOURCE_KIND),disabled)
BASELINE_SYNC_TARGET := baseline-sync-disabled
BASELINE_SYNC_CHECK_TARGET := baseline-sync-check-disabled
else
BASELINE_SYNC_TARGET := baseline-sync-invalid
BASELINE_SYNC_CHECK_TARGET := baseline-sync-invalid
endif

ifeq ($(BASELINE_SOURCE_KIND),aiscb)
BASELINE_SYNC_LATEST_TARGET := baseline-sync-latest-aiscb
else ifeq ($(BASELINE_SOURCE_KIND),organization)
BASELINE_SYNC_LATEST_TARGET := baseline-sync-latest-organization
else ifeq ($(BASELINE_SOURCE_KIND),organization-git)
BASELINE_SYNC_LATEST_TARGET := baseline-sync-latest-organization-git
else ifeq ($(BASELINE_SOURCE_KIND),organization-https)
BASELINE_SYNC_LATEST_TARGET := baseline-sync-latest-organization-https
else ifeq ($(BASELINE_SOURCE_KIND),disabled)
BASELINE_SYNC_LATEST_TARGET := baseline-sync-latest-disabled
else
BASELINE_SYNC_LATEST_TARGET := baseline-sync-latest-invalid
endif

.DEFAULT_GOAL := help

.PHONY: help lint check release release-check fetch-upstream upstream-check packaging-template-check baseline-check baseline-sync baseline-sync-check baseline-sync-latest baseline-sync-aiscb baseline-sync-check-aiscb baseline-sync-latest-aiscb baseline-sync-organization baseline-sync-check-organization baseline-sync-latest-organization baseline-sync-organization-git baseline-sync-check-organization-git baseline-sync-latest-organization-git baseline-sync-organization-https baseline-sync-check-organization-https baseline-sync-latest-organization-https baseline-sync-disabled baseline-sync-check-disabled baseline-sync-latest-disabled baseline-sync-invalid baseline-sync-latest-invalid check-updates drift-check upstream-update org-baseline-sync org-baseline-sync-check validate package release-package package-archive local-marketplace install-local smoke ci-github ci-gitlab clean rebuild reinit test

help: ## Show this help
	@if [ -t 1 ] && [ -z "$${NO_COLOR:-}" ]; then \
		hdr=$$(printf '\033[1m'); tgt=$$(printf '\033[36m'); off=$$(printf '\033[0m'); \
	fi; \
	awk -v hdr="$$hdr" -v tgt="$$tgt" -v off="$$off" 'BEGIN {FS = ":.*## "} \
		/^##@ / {printf "\n%s%s%s\n", hdr, substr($$0, 5), off; next} \
		/^[a-zA-Z0-9_-]+:.*## / {printf "  %s%-26s%s %s\n", tgt, $$1, off, $$2}' $(MAKEFILE_LIST)
	@printf '\nCurrent settings (override on the command line or via env):\n'
	@printf '  INTERNAL_NAME=%s\n  PACKAGE_VERSION=%s\n  APPSEC_ADVISOR_REF=%s\n  APPSEC_ADVISOR_TEMPLATE_REF=%s\n  BASELINE_SOURCE_KIND=%s\n' \
		'$(INTERNAL_NAME)' '$(PACKAGE_VERSION)' '$(APPSEC_ADVISOR_REF)' '$(APPSEC_ADVISOR_TEMPLATE_REF)' '$(BASELINE_SOURCE_KIND)'
	@case '$(BASELINE_SOURCE_KIND)' in \
		organization-git) \
			printf '  ORG_BASELINE_URL=%s\n  ORG_BASELINE_REF=%s\n  ORG_BASELINE_SOURCE=%s (optional local override)\n  ORG_BASELINE_DOC=%s\n  ORG_BASELINE_SKILLS_DIR=%s\n' \
				'$(ORG_BASELINE_URL)' '$(ORG_BASELINE_REF)' '$(ORG_BASELINE_SOURCE)' '$(ORG_BASELINE_DOC)' '$(ORG_BASELINE_SKILLS_DIR)' ;; \
		organization-https) printf '  ORG_BASELINE_URL=%s\n' '$(ORG_BASELINE_URL)' ;; \
		organization) \
			printf '  ORG_BASELINE_SOURCE=%s\n  ORG_BASELINE_DOC=%s\n  ORG_BASELINE_SKILLS_DIR=%s\n' \
				'$(ORG_BASELINE_SOURCE)' '$(ORG_BASELINE_DOC)' '$(ORG_BASELINE_SKILLS_DIR)' ;; \
	esac
	@printf '\nBaseline sources:\n'
	@printf '  aiscb               Sync the generic AISCB document configured in org-profile.yaml.\n'
	@printf '  organization-git    Temporarily fetch URL/ref; copy the composed document and optional skills.\n'
	@printf '  organization-https  Download exactly one composed HTTPS document; no skills or archives.\n'
	@printf '  disabled             Skip baseline checks and synchronization.\n'
	@printf 'Packaging always uses the tracked reviewed copies; it never syncs a baseline implicitly.\n'
	@printf 'Use baseline-sync-check before baseline-sync. A new baseline id requires ACCEPT_ID=<id>.\n'
	@printf 'Synced org skills ship only after explicit inclusion in org-profile/package-policy.yaml.\n'

##@ Build

package: $(FETCH_TARGET) ## Fetch + build + smoke-test the plugin into build/<name>/
	@$(PACKAGE_ENV) scripts/package-local.sh

rebuild: clean package ## clean then package

validate: $(FETCH_TARGET) ## Validate org-profile.yaml against the upstream schema
	@echo "==> Validating org-profile.yaml against $(APPSEC_ADVISOR_REF)"
	@python3 "$(APPSEC_ADVISOR_SOURCE)/scripts/validate_org_profile.py" org-profile/org-profile.yaml
	@PYTHONDONTWRITEBYTECODE=1 python3 scripts/check-org-hook-collisions.py --source "$(APPSEC_ADVISOR_SOURCE)" --profile org-profile/org-profile.yaml

fetch-upstream: ## Clone/checkout upstream appsec-advisor at APPSEC_ADVISOR_REF
	@$(UPSTREAM_ENV) scripts/fetch-upstream.sh

smoke: $(FETCH_TARGET) ## Smoke-test an already-built package
	@echo "==> Smoke-testing build/$(INTERNAL_NAME)"
	@python3 "$(APPSEC_ADVISOR_SOURCE)/scripts/smoke_test_package.py" "build/$(INTERNAL_NAME)" --name "$(INTERNAL_NAME)"

##@ Try it out locally

local-marketplace: package ## Prepare build/ as a local Claude Code marketplace
	python3 scripts/prepare-local-marketplace.py --build-root build --plugin-name "$(INTERNAL_NAME)" --marketplace-name "$(LOCAL_MARKETPLACE_NAME)"

install-local: local-marketplace ## Register the local marketplace and install the built plugin
	@command -v claude >/dev/null 2>&1 || { echo "ERROR: claude CLI not found" >&2; exit 2; }
	claude plugin marketplace add "$(abspath build)" --scope "$(LOCAL_MARKETPLACE_SCOPE)"
	claude plugin install "$(INTERNAL_NAME)@$(LOCAL_MARKETPLACE_NAME)" --scope "$(LOCAL_MARKETPLACE_SCOPE)"

##@ Check

check: lint test ## Offline gate: lint + test (no network, no upstream fetch)
	@echo "OK: offline checks passed"

lint: ## shellcheck the shell scripts (skipped if shellcheck is absent)
	@if command -v shellcheck >/dev/null 2>&1; then \
		echo "==> shellcheck scripts/"; \
		shellcheck scripts/*.sh $$([ -f tests/run.sh ] && echo tests/run.sh); \
	else \
		echo "NOTE: shellcheck not installed — skipping (see https://github.com/koalaman/shellcheck#installing)"; \
	fi

test: ## Run the shell-script test suite + coverage gate (skipped if tests/ is absent)
	@if [ -f tests/run.sh ]; then \
		bash tests/run.sh; \
	else \
		echo "NOTE: no tests/ in this repo — skipping"; \
	fi

##@ Upstream and template

upstream-check: ## Read-only drift check for the appsec-advisor ref and releases
	@status=0; \
	$(UPSTREAM_ENV) scripts/upstream-check.sh || status=$$?; \
	if [ "$$status" -eq 1 ]; then \
		echo "$(DRIFT_NOTE)"; \
	fi; \
	exit $$status

# 'upstream-check' reports commit drift and release drift through the same exit
# status, but only commit drift is something a rebuild fixes: a newer release
# tag is built only after APPSEC_ADVISOR_REF is raised by hand. So decide on the
# kind of drift reported, not on the exit status alone.
upstream-update: ## Rebuild only when the selected upstream ref moved to a new commit
	@status=0; \
	out="$$($(UPSTREAM_ENV) scripts/upstream-check.sh 2>&1)" || status=$$?; \
	printf '%s\n' "$$out"; \
	if [ "$$status" -ge 2 ]; then \
		echo "ERROR: upstream check could not complete — nothing rebuilt" >&2; exit 2; \
	fi; \
	if printf '%s\n' "$$out" | grep -q '^DRIFT (release)'; then \
		echo "NOTE: a newer release exists — raise APPSEC_ADVISOR_REF in the Makefile to build it"; \
	fi; \
	if printf '%s\n' "$$out" | grep -qE '^(DRIFT \(commit\)|NOTE: no local checkout)'; then \
		echo "==> rebuilding from $(APPSEC_ADVISOR_REF)"; \
		$(MAKE) --no-print-directory rebuild; \
	else \
		echo "OK: nothing to rebuild"; \
	fi

check-updates: $(FETCH_TARGET) ## Check appsec-advisor and secure-coding baseline for updates
	@status=0; \
	$(UPSTREAM_ENV) scripts/upstream-check.sh || status=$$?; \
	echo; \
	baseline_status=0; baseline_make_status=0; \
	baseline_out="$$( $(MAKE) --no-print-directory baseline-sync-check 2>&1 )" || baseline_make_status=$$?; \
	printf '%s\n' "$$baseline_out"; \
	if [ "$$baseline_make_status" -ne 0 ]; then \
		if printf '%s\n' "$$baseline_out" | grep -q '^ERROR:'; then baseline_status=2; else baseline_status=1; fi; \
	fi; \
	if [ "$$status" -eq 2 ] || [ "$$baseline_status" -eq 2 ]; then \
		echo "ERROR: at least one upstream check could not complete" >&2; exit 2; \
	fi; \
	if [ "$$status" -eq 1 ] || [ "$$baseline_status" -eq 1 ]; then \
		echo "DRIFT: at least one upstream source has changed"; \
		echo "$(DRIFT_NOTE)"; exit 1; \
	fi; \
	echo "OK: appsec-advisor and baseline are current"

# Public baseline targets dispatch to the source kind persisted by the
# initializer. All source kinds have the same exit contract and ACCEPT_ID
# acknowledgement; callers and CI do not need source-specific commands.
BASELINE_SYNC := "$(APPSEC_ADVISOR_SOURCE)/scripts/sync_baseline.py"
BASELINE_SYNC_MISSING := \
	echo "ERROR: the selected upstream ($(APPSEC_ADVISOR_REF)) has no sync_baseline.py --profile" >&2; \
	echo "Pin APPSEC_ADVISOR_REF to a ref that carries it — a release tag or a branch such as dev." >&2; \
	exit 2

baseline-sync: $(BASELINE_SYNC_TARGET) ## Sync baseline.file and optional org skills (ACCEPT_ID=<id> for a new id)

baseline-sync-check: $(BASELINE_SYNC_CHECK_TARGET) ## Read-only drift check for the configured baseline source

# Kept as a familiar read-only alias: it now checks both id and bytes, plus org
# skills when organization mode is selected.
baseline-check: baseline-sync-check ## Read-only drift check for the configured secure-coding baseline

# Convenience wrapper around baseline-sync: whatever id the source currently
# publishes is accepted without a human typing it back. This skips the review
# gate baseline-sync exists to enforce (see the comment above BASELINE_SYNC) —
# use it only where blindly trusting the source's current content is
# acceptable, not as the default team workflow. Dispatches by
# BASELINE_SOURCE_KIND like baseline-sync itself.
baseline-sync-latest: $(BASELINE_SYNC_LATEST_TARGET) ## Re-vendor the baseline, auto-accepting whatever id the source currently publishes (skips the review gate)

baseline-sync-aiscb: $(FETCH_TARGET)
	@python3 $(BASELINE_SYNC) --help 2>/dev/null | grep -q -- --profile || { $(BASELINE_SYNC_MISSING); }; \
	status=0; \
	python3 $(BASELINE_SYNC) --profile org-profile/org-profile.yaml \
		$${ACCEPT_ID:+--accept-id "$${ACCEPT_ID}"} || status=$$?; \
	if [ "$$status" -eq 3 ]; then \
		echo "NOTE: a new baseline id is a decision — re-run with ACCEPT_ID=<id> to move file and profile together."; \
	fi; \
	exit $$status

baseline-sync-latest-aiscb: $(FETCH_TARGET)
	@python3 $(BASELINE_SYNC) --help 2>/dev/null | grep -q -- --profile || { $(BASELINE_SYNC_MISSING); }; \
	out="$$(python3 $(BASELINE_SYNC) --profile org-profile/org-profile.yaml 2>&1)"; status=$$?; \
	if [ "$$status" -eq 3 ]; then \
		published="$$(printf '%s\n' "$$out" | grep -oE "publishes '?[A-Za-z0-9][A-Za-z0-9._+-]*'?" | head -1)"; \
		published="$${published#publishes }"; published="$${published%\'}"; published="$${published#\'}"; \
		if [ -z "$$published" ]; then \
			echo "ERROR: could not parse the published id from sync output" >&2; \
			printf '%s\n' "$$out" >&2; \
			exit 2; \
		fi; \
		echo "NOTE: auto-accepting published id $$published"; \
		python3 $(BASELINE_SYNC) --profile org-profile/org-profile.yaml --accept-id "$$published"; \
		exit $$?; \
	fi; \
	printf '%s\n' "$$out"; \
	exit $$status

baseline-sync-check-aiscb: $(FETCH_TARGET)
	@python3 $(BASELINE_SYNC) --help 2>/dev/null | grep -q -- --profile || { $(BASELINE_SYNC_MISSING); }; \
	status=0; \
	out="$$(python3 $(BASELINE_SYNC) --profile org-profile/org-profile.yaml --dry-run 2>&1)" || status=$$?; \
	printf '%s\n' "$$out"; \
	if [ "$$status" -eq 3 ]; then \
		echo "DRIFT (baseline text): the source publishes a new id"; \
		echo "$(DRIFT_NOTE)"; exit 1; \
	fi; \
	if [ "$$status" -ne 0 ]; then exit 2; fi; \
	if printf '%s\n' "$$out" | grep -q '^would write'; then \
		echo "DRIFT (baseline text): the vendored copy no longer matches its source under the same id"; \
		echo "$(DRIFT_NOTE)"; exit 1; \
	fi; \
	exit 0

ORG_BASELINE_CONFIG_MISSING := \
	echo "ERROR: organization baseline mode requires ORG_BASELINE_SOURCE and ORG_BASELINE_DOC." >&2; \
	exit 2

ORG_BASELINE_GIT_CONFIG_MISSING := \
	echo "ERROR: organization-git mode requires ORG_BASELINE_URL, ORG_BASELINE_REF, and ORG_BASELINE_DOC (or ORG_BASELINE_SOURCE as a local checkout override)." >&2; \
	exit 2

ORG_BASELINE_HTTPS_CONFIG_MISSING := \
	echo "ERROR: organization-https mode requires ORG_BASELINE_URL and permits no skills directory." >&2; \
	exit 2

baseline-sync-check-organization:
	@if [ -z "$(ORG_BASELINE_SOURCE)" ] || [ -z "$(ORG_BASELINE_DOC)" ]; then $(ORG_BASELINE_CONFIG_MISSING); fi; \
	skills_dir="$(ORG_BASELINE_SKILLS_DIR)"; status=0; \
	python3 scripts/sync-org-baseline.py --org-profile org-profile --org-skills org-skills \
		--checkout "$(ORG_BASELINE_SOURCE)" --doc "$(ORG_BASELINE_DOC)" \
		$${skills_dir:+--skills-dir "$$skills_dir"} || status=$$?; \
	if [ "$$status" -eq 1 ]; then echo "$(DRIFT_NOTE)"; fi; \
	exit $$status

baseline-sync-organization:
	@if [ -z "$(ORG_BASELINE_SOURCE)" ] || [ -z "$(ORG_BASELINE_DOC)" ]; then $(ORG_BASELINE_CONFIG_MISSING); fi; \
	skills_dir="$(ORG_BASELINE_SKILLS_DIR)"; status=0; \
	python3 scripts/sync-org-baseline.py --org-profile org-profile --org-skills org-skills \
		--checkout "$(ORG_BASELINE_SOURCE)" --doc "$(ORG_BASELINE_DOC)" \
		$${skills_dir:+--skills-dir "$$skills_dir"} \
		$${ACCEPT_ID:+--accept-id "$$ACCEPT_ID"} --write || status=$$?; \
	if [ "$$status" -eq 3 ]; then \
		echo "NOTE: a new baseline id is a decision — re-run with ACCEPT_ID=<id> to move file and profile together."; \
	fi; \
	exit $$status

baseline-sync-latest-organization:
	@if [ -z "$(ORG_BASELINE_SOURCE)" ] || [ -z "$(ORG_BASELINE_DOC)" ]; then $(ORG_BASELINE_CONFIG_MISSING); fi; \
	skills_dir="$(ORG_BASELINE_SKILLS_DIR)"; \
	out="$$(python3 scripts/sync-org-baseline.py --org-profile org-profile --org-skills org-skills \
		--checkout "$(ORG_BASELINE_SOURCE)" --doc "$(ORG_BASELINE_DOC)" \
		$${skills_dir:+--skills-dir "$$skills_dir"} --write 2>&1)"; status=$$?; \
	if [ "$$status" -eq 3 ]; then \
		published="$$(printf '%s\n' "$$out" | grep -oE "publishes '?[A-Za-z0-9][A-Za-z0-9._+-]*'?" | head -1)"; \
		published="$${published#publishes }"; published="$${published%\'}"; published="$${published#\'}"; \
		if [ -z "$$published" ]; then \
			echo "ERROR: could not parse the published id from sync output" >&2; \
			printf '%s\n' "$$out" >&2; \
			exit 2; \
		fi; \
		echo "NOTE: auto-accepting published id $$published"; \
		python3 scripts/sync-org-baseline.py --org-profile org-profile --org-skills org-skills \
			--checkout "$(ORG_BASELINE_SOURCE)" --doc "$(ORG_BASELINE_DOC)" \
			$${skills_dir:+--skills-dir "$$skills_dir"} --accept-id "$$published" --write; \
		exit $$?; \
	fi; \
	printf '%s\n' "$$out"; \
	exit $$status

baseline-sync-check-organization-git:
	@if { [ -z "$(ORG_BASELINE_SOURCE)" ] && [ -z "$(ORG_BASELINE_URL)" ]; } || [ -z "$(ORG_BASELINE_REF)" ] || [ -z "$(ORG_BASELINE_DOC)" ]; then $(ORG_BASELINE_GIT_CONFIG_MISSING); fi; \
	set -- --org-profile org-profile --org-skills org-skills --manage-skills; \
	if [ -n "$(ORG_BASELINE_SOURCE)" ]; then set -- "$$@" --checkout "$(ORG_BASELINE_SOURCE)"; else set -- "$$@" --git-url "$(ORG_BASELINE_URL)" --git-ref "$(ORG_BASELINE_REF)"; fi; \
	set -- "$$@" --doc "$(ORG_BASELINE_DOC)"; \
	if [ -n "$(ORG_BASELINE_SKILLS_DIR)" ]; then set -- "$$@" --skills-dir "$(ORG_BASELINE_SKILLS_DIR)"; fi; \
	status=0; python3 scripts/sync-org-baseline.py "$$@" || status=$$?; \
	if [ "$$status" -eq 1 ]; then echo "$(DRIFT_NOTE)"; fi; \
	exit $$status

baseline-sync-organization-git:
	@if { [ -z "$(ORG_BASELINE_SOURCE)" ] && [ -z "$(ORG_BASELINE_URL)" ]; } || [ -z "$(ORG_BASELINE_REF)" ] || [ -z "$(ORG_BASELINE_DOC)" ]; then $(ORG_BASELINE_GIT_CONFIG_MISSING); fi; \
	set -- --org-profile org-profile --org-skills org-skills --manage-skills; \
	if [ -n "$(ORG_BASELINE_SOURCE)" ]; then set -- "$$@" --checkout "$(ORG_BASELINE_SOURCE)"; else set -- "$$@" --git-url "$(ORG_BASELINE_URL)" --git-ref "$(ORG_BASELINE_REF)"; fi; \
	set -- "$$@" --doc "$(ORG_BASELINE_DOC)"; \
	if [ -n "$(ORG_BASELINE_SKILLS_DIR)" ]; then set -- "$$@" --skills-dir "$(ORG_BASELINE_SKILLS_DIR)"; fi; \
	if [ -n "$${ACCEPT_ID:-}" ]; then set -- "$$@" --accept-id "$$ACCEPT_ID"; fi; \
	status=0; python3 scripts/sync-org-baseline.py "$$@" --write || status=$$?; \
	if [ "$$status" -eq 3 ]; then echo "NOTE: a new baseline id is a decision — re-run with ACCEPT_ID=<id> to move file and profile together."; fi; \
	exit $$status

baseline-sync-latest-organization-git:
	@if { [ -z "$(ORG_BASELINE_SOURCE)" ] && [ -z "$(ORG_BASELINE_URL)" ]; } || [ -z "$(ORG_BASELINE_REF)" ] || [ -z "$(ORG_BASELINE_DOC)" ]; then $(ORG_BASELINE_GIT_CONFIG_MISSING); fi; \
	set -- --org-profile org-profile --org-skills org-skills --manage-skills; \
	if [ -n "$(ORG_BASELINE_SOURCE)" ]; then set -- "$$@" --checkout "$(ORG_BASELINE_SOURCE)"; else set -- "$$@" --git-url "$(ORG_BASELINE_URL)" --git-ref "$(ORG_BASELINE_REF)"; fi; \
	set -- "$$@" --doc "$(ORG_BASELINE_DOC)"; \
	if [ -n "$(ORG_BASELINE_SKILLS_DIR)" ]; then set -- "$$@" --skills-dir "$(ORG_BASELINE_SKILLS_DIR)"; fi; \
	out="$$(python3 scripts/sync-org-baseline.py "$$@" --write 2>&1)"; status=$$?; \
	if [ "$$status" -eq 3 ]; then \
		published="$$(printf '%s\n' "$$out" | grep -oE "publishes '?[A-Za-z0-9][A-Za-z0-9._+-]*'?" | head -1)"; \
		published="$${published#publishes }"; published="$${published%\'}"; published="$${published#\'}"; \
		if [ -z "$$published" ]; then \
			echo "ERROR: could not parse the published id from sync output" >&2; \
			printf '%s\n' "$$out" >&2; \
			exit 2; \
		fi; \
		echo "NOTE: auto-accepting published id $$published"; \
		python3 scripts/sync-org-baseline.py "$$@" --accept-id "$$published" --write; \
		exit $$?; \
	fi; \
	printf '%s\n' "$$out"; \
	exit $$status

baseline-sync-check-organization-https:
	@if [ -z "$(ORG_BASELINE_URL)" ] || [ -n "$(ORG_BASELINE_SKILLS_DIR)" ]; then $(ORG_BASELINE_HTTPS_CONFIG_MISSING); fi; \
	status=0; python3 scripts/sync-org-baseline.py --org-profile org-profile --org-skills org-skills \
		--https-url "$(ORG_BASELINE_URL)" || status=$$?; \
	if [ "$$status" -eq 1 ]; then echo "$(DRIFT_NOTE)"; fi; \
	exit $$status

baseline-sync-organization-https:
	@if [ -z "$(ORG_BASELINE_URL)" ] || [ -n "$(ORG_BASELINE_SKILLS_DIR)" ]; then $(ORG_BASELINE_HTTPS_CONFIG_MISSING); fi; \
	set -- --org-profile org-profile --org-skills org-skills --https-url "$(ORG_BASELINE_URL)"; \
	if [ -n "$${ACCEPT_ID:-}" ]; then set -- "$$@" --accept-id "$$ACCEPT_ID"; fi; \
	status=0; python3 scripts/sync-org-baseline.py "$$@" --write || status=$$?; \
	if [ "$$status" -eq 3 ]; then echo "NOTE: a new baseline id is a decision — re-run with ACCEPT_ID=<id> to move file and profile together."; fi; \
	exit $$status

baseline-sync-latest-organization-https:
	@if [ -z "$(ORG_BASELINE_URL)" ] || [ -n "$(ORG_BASELINE_SKILLS_DIR)" ]; then $(ORG_BASELINE_HTTPS_CONFIG_MISSING); fi; \
	out="$$(python3 scripts/sync-org-baseline.py --org-profile org-profile --org-skills org-skills \
		--https-url "$(ORG_BASELINE_URL)" --write 2>&1)"; status=$$?; \
	if [ "$$status" -eq 3 ]; then \
		published="$$(printf '%s\n' "$$out" | grep -oE "publishes '?[A-Za-z0-9][A-Za-z0-9._+-]*'?" | head -1)"; \
		published="$${published#publishes }"; published="$${published%\'}"; published="$${published#\'}"; \
		if [ -z "$$published" ]; then \
			echo "ERROR: could not parse the published id from sync output" >&2; \
			printf '%s\n' "$$out" >&2; \
			exit 2; \
		fi; \
		echo "NOTE: auto-accepting published id $$published"; \
		python3 scripts/sync-org-baseline.py --org-profile org-profile --org-skills org-skills \
			--https-url "$(ORG_BASELINE_URL)" --accept-id "$$published" --write; \
		exit $$?; \
	fi; \
	printf '%s\n' "$$out"; \
	exit $$status

baseline-sync-disabled:
	@echo "SKIP: secure-coding baseline is disabled"

baseline-sync-check-disabled:
	@echo "SKIP: secure-coding baseline is disabled"

baseline-sync-latest-disabled:
	@echo "SKIP: secure-coding baseline is disabled"

baseline-sync-invalid:
	@echo "ERROR: BASELINE_SOURCE_KIND must be aiscb, organization-git, organization-https, or disabled; got '$(BASELINE_SOURCE_KIND)'" >&2
	@exit 2

baseline-sync-latest-invalid:
	@echo "ERROR: BASELINE_SOURCE_KIND must be aiscb, organization-git, organization-https, or disabled; got '$(BASELINE_SOURCE_KIND)'" >&2
	@exit 2

# Compatibility aliases for repositories that adopted the earlier WIP names.
org-baseline-sync-check: baseline-sync-check

org-baseline-sync: baseline-sync

packaging-template-check: ## Read-only check for a newer packaging-template revision
	@status=0; \
	APPSEC_ADVISOR_TEMPLATE_URL="$(APPSEC_ADVISOR_TEMPLATE_URL)" APPSEC_ADVISOR_TEMPLATE_REF="$(APPSEC_ADVISOR_TEMPLATE_REF)" scripts/packaging-template-check.sh || status=$$?; \
	if [ "$$status" -eq 1 ]; then \
		echo "$(DRIFT_NOTE)"; \
	fi; \
	exit $$status

# Deprecated alias for check-updates; kept working, left out of `make help`.
drift-check:
	@$(MAKE) --no-print-directory check-updates

##@ Release

release-check: ## Release-boundary gate: check + validate + build a clean plugin against upstream
	@echo "==> [1/5] check (lint + test)"
	@$(MAKE) --no-print-directory check
	# Not in `make check`: the documented workflow commits the initializer first
	# and repins the README after, so the two disagree in between. A release must
	# not carry that state. Guarded because a scaffolded repository has no such
	# pin and does not ship the script.
	@if [ -f scripts/check-quickstart-pin.py ]; then \
		echo "==> [2/5] quick-start pin"; \
		python3 scripts/check-quickstart-pin.py; \
	else \
		echo "==> [2/5] quick-start pin (not applicable)"; \
	fi
	@echo "==> [3/5] check-updates (advisory)"
	@$(MAKE) --no-print-directory check-updates || echo "NOTE: update available or check error (advisory) — not blocking release-check"
	@echo "==> [4/5] validate"
	@$(MAKE) --no-print-directory validate
	@echo "==> [5/5] package"
	@$(MAKE) --no-print-directory package
	@echo "OK: release-check passed"

release-package: $(FETCH_TARGET) ## Build distributable .tgz/.zip archives with checksums
	@echo "==> Building distributable archives for $(INTERNAL_NAME)"
	@$(PACKAGE_ENV) ARCHIVE=1 scripts/package-local.sh

# Deprecated alias for release-package; kept working, left out of `make help`.
package-archive: release-package

release: ## Validate, tag and push a release (RELEASE_VERSION=x.y.z)
	@scripts/release.sh "$(RELEASE_VERSION)"

##@ Maintenance

clean: ## Remove generated dirs, coverage output and local tool caches
	rm -rf upstream/ build/ dist/ .ruff_cache/ scripts/__pycache__/ tests/__pycache__/ org-profile/hooks/__pycache__/ coverage.lcov

reinit: ## Reapply the selected template ref using the existing settings
	scripts/reinit-org-repo.sh

ci-github: ## Install the GitHub Actions packaging workflow
	mkdir -p .github/workflows
	cp ci-templates/github/workflows/package.yml .github/workflows/package.yml

ci-gitlab: ## Install the GitLab CI config
	cp ci-templates/gitlab-ci.yml .gitlab-ci.yml
