.PHONY: help build-k8s-runtime build-k8s-agent push-k8s-runtime push-k8s-agent check-current-state create-r10k-secret create-license-secret deploy-eyrie-pe deploy-eyrie-agent deploy-eyrie lint

CONTAINER_ENGINE ?= podman
PE_VERSION ?=
PE_NAMESPACE ?= puppet
PE_RELEASE ?= pe
PE_AGENT_RELEASE ?= test-node
LOCAL_DIR ?= local
ARTIFACTS_DIR ?= artifacts
PE_VALUES_FILE ?= $(LOCAL_DIR)/values-eyrie.yaml
PE_AGENT_VALUES_FILE ?= $(LOCAL_DIR)/values-agent-eyrie.yaml
LOCAL_KEYS_DIR ?= $(LOCAL_DIR)/keys
R10K_DEPLOY_KEY_PATH ?= $(LOCAL_KEYS_DIR)/id-control_repo.ed25519
R10K_DEPLOY_KEY_SECRET_NAME ?= pe-r10k-deploy-key
PE_LICENSE_PATH ?= $(LOCAL_DIR)/license.txt
PE_LICENSE_SECRET_NAME ?= pe-license
PE_INSTALLERS_DIR ?= $(ARTIFACTS_DIR)/pe-installers
PE_INSTALLER_FILENAME ?= puppet-enterprise-$(PE_VERSION)-el-9-x86_64.tar.gz
PE_INSTALLER_TAR_PATH ?= $(abspath $(PE_INSTALLERS_DIR)/$(PE_INSTALLER_FILENAME))
K8S_RUNTIME_IMAGE_NAME ?= registry.eyrie/pe-k8s-runtime
K8S_RUNTIME_IMAGE_VERSION ?= $(PE_VERSION)
K8S_INSTALLER_CONTEXT_PATH ?= image/assets/pe-installer/installer.tar.gz
K8S_AGENT_IMAGE_NAME ?= registry.eyrie/pe-k8s-agent
K8S_AGENT_IMAGE_VERSION ?= dev

help:
	@echo "Puppet Enterprise on Kubernetes"
	@echo ""
	@echo "Build targets:"
	@echo "  make build-k8s-runtime PE_VERSION=<version>"
	@echo "  make push-k8s-runtime PE_VERSION=<version>"
	@echo "  make build-k8s-agent K8S_AGENT_IMAGE_VERSION=<version>"
	@echo "  make push-k8s-agent K8S_AGENT_IMAGE_VERSION=<version>"
	@echo ""
	@echo "Deploy current eyrie state:"
	@echo "  make check-current-state PE_VERSION=<version>"
	@echo "  make deploy-eyrie-pe"
	@echo "  make deploy-eyrie-agent"
	@echo "  make deploy-eyrie"
	@echo ""
	@echo "Repo-local artifact paths:"
	@echo "  installer: $(PE_INSTALLERS_DIR)/puppet-enterprise-<version>-el-9-x86_64.tar.gz"
	@echo "  pe values: $(PE_VALUES_FILE)"
	@echo "  agent values: $(PE_AGENT_VALUES_FILE)"
	@echo "  r10k key: $(R10K_DEPLOY_KEY_PATH)"
	@echo "  license: $(PE_LICENSE_PATH) (optional)"
	@echo ""
	@echo "Validation targets:"
	@echo "  make lint"

build-k8s-runtime:
	@if [ -z "$(CONTAINER_ENGINE)" ]; then \
		echo "ERROR: CONTAINER_ENGINE is required"; \
		exit 1; \
	fi
	@if [ -z "$(PE_VERSION)" ] || [ -z "$(PE_INSTALLER_TAR_PATH)" ]; then \
		echo "ERROR: PE_VERSION and PE_INSTALLER_TAR_PATH are required"; \
		echo "Usage: make build-k8s-runtime PE_VERSION=<version> [PE_INSTALLER_TAR_PATH=/absolute/path/to/installer.tar.gz]"; \
		exit 1; \
	fi
	@if [ ! -f "$(PE_INSTALLER_TAR_PATH)" ]; then \
		echo "ERROR: Installer file not found: $(PE_INSTALLER_TAR_PATH)"; \
		exit 1; \
	fi
	@if ! tar -tzf "$(PE_INSTALLER_TAR_PATH)" > /dev/null 2>&1; then \
		echo "ERROR: PE_INSTALLER_TAR_PATH is not a valid tar.gz archive: $(PE_INSTALLER_TAR_PATH)"; \
		exit 1; \
	fi
	@case "$(PE_INSTALLER_TAR_PATH)" in \
		/*) ;; \
		*) echo "ERROR: PE_INSTALLER_TAR_PATH must be an absolute path: $(PE_INSTALLER_TAR_PATH)"; exit 1 ;; \
	esac
	@mkdir -p "$(dir $(K8S_INSTALLER_CONTEXT_PATH))"
	@echo "[Build] Staging installer into image context: $(K8S_INSTALLER_CONTEXT_PATH)"
	@cp -f "$(PE_INSTALLER_TAR_PATH)" "$(K8S_INSTALLER_CONTEXT_PATH)"
	@echo "[Build] Starting $(CONTAINER_ENGINE) build for $(K8S_RUNTIME_IMAGE_NAME):$(K8S_RUNTIME_IMAGE_VERSION)"
	@set -e; \
	"$(CONTAINER_ENGINE)" build \
		--build-arg PE_VERSION="$(PE_VERSION)" \
		--tag "$(K8S_RUNTIME_IMAGE_NAME):$(K8S_RUNTIME_IMAGE_VERSION)" \
		--tag "$(K8S_RUNTIME_IMAGE_NAME):latest" \
		--file image/Containerfile \
		.; \
	status=$$?; \
	rm -f "$(K8S_INSTALLER_CONTEXT_PATH)"; \
	if [ $$status -ne 0 ]; then \
		echo "[Build] FAILED"; \
		exit $$status; \
	fi

build-k8s-agent:
	@if [ -z "$(CONTAINER_ENGINE)" ]; then \
		echo "ERROR: CONTAINER_ENGINE is required"; \
		exit 1; \
	fi
	@echo "[Build] Starting $(CONTAINER_ENGINE) build for $(K8S_AGENT_IMAGE_NAME):$(K8S_AGENT_IMAGE_VERSION)"
	@"$(CONTAINER_ENGINE)" build \
		--build-arg K8S_AGENT_IMAGE_VERSION="$(K8S_AGENT_IMAGE_VERSION)" \
		--tag "$(K8S_AGENT_IMAGE_NAME):$(K8S_AGENT_IMAGE_VERSION)" \
		--tag "$(K8S_AGENT_IMAGE_NAME):latest" \
		--file agent-image/Containerfile \
		.

push-k8s-runtime:
	@if [ -z "$(CONTAINER_ENGINE)" ]; then \
		echo "ERROR: CONTAINER_ENGINE is required"; \
		exit 1; \
	fi
	@echo "[Push] Pushing $(K8S_RUNTIME_IMAGE_NAME):$(K8S_RUNTIME_IMAGE_VERSION)"
	@"$(CONTAINER_ENGINE)" push "$(K8S_RUNTIME_IMAGE_NAME):$(K8S_RUNTIME_IMAGE_VERSION)"

push-k8s-agent:
	@if [ -z "$(CONTAINER_ENGINE)" ]; then \
		echo "ERROR: CONTAINER_ENGINE is required"; \
		exit 1; \
	fi
	@echo "[Push] Pushing $(K8S_AGENT_IMAGE_NAME):$(K8S_AGENT_IMAGE_VERSION)"
	@"$(CONTAINER_ENGINE)" push "$(K8S_AGENT_IMAGE_NAME):$(K8S_AGENT_IMAGE_VERSION)"

check-current-state:
	@missing=0; \
	for path in "$(PE_VALUES_FILE)" "$(PE_AGENT_VALUES_FILE)" "$(R10K_DEPLOY_KEY_PATH)"; do \
		if [ -f "$$path" ]; then \
			echo "[OK] $$path"; \
		else \
			echo "[MISSING] $$path"; \
			missing=1; \
		fi; \
	done; \
	if [ -n "$(PE_VERSION)" ]; then \
		if [ -f "$(PE_INSTALLER_TAR_PATH)" ]; then \
			echo "[OK] $(PE_INSTALLER_TAR_PATH)"; \
		else \
			echo "[MISSING] $(PE_INSTALLER_TAR_PATH)"; \
			missing=1; \
		fi; \
	else \
		echo "[INFO] Set PE_VERSION to validate installer presence under $(PE_INSTALLERS_DIR)"; \
	fi; \
	if [ -f "$(PE_LICENSE_PATH)" ]; then \
		echo "[OK] $(PE_LICENSE_PATH) (optional)"; \
	else \
		echo "[OPTIONAL] $(PE_LICENSE_PATH)"; \
	fi; \
	exit $$missing

create-r10k-secret:
	@if [ ! -f "$(R10K_DEPLOY_KEY_PATH)" ]; then \
		echo "ERROR: R10K deploy key not found: $(R10K_DEPLOY_KEY_PATH)"; \
		exit 1; \
	fi
	kubectl -n "$(PE_NAMESPACE)" create secret generic "$(R10K_DEPLOY_KEY_SECRET_NAME)" \
		--from-file=r10k-deploy-key="$(R10K_DEPLOY_KEY_PATH)" \
		--dry-run=client -o yaml | kubectl apply -f -

create-license-secret:
	@if [ ! -f "$(PE_LICENSE_PATH)" ]; then \
		echo "ERROR: PE license file not found: $(PE_LICENSE_PATH)"; \
		exit 1; \
	fi
	kubectl -n "$(PE_NAMESPACE)" create secret generic "$(PE_LICENSE_SECRET_NAME)" \
		--from-file=license.txt="$(PE_LICENSE_PATH)" \
		--dry-run=client -o yaml | kubectl apply -f -

deploy-eyrie-pe: create-r10k-secret
	@if [ ! -f "$(PE_VALUES_FILE)" ]; then \
		echo "ERROR: PE values file not found: $(PE_VALUES_FILE)"; \
		exit 1; \
	fi
	helm upgrade --install "$(PE_RELEASE)" charts/puppet-enterprise \
		--namespace "$(PE_NAMESPACE)" \
		--create-namespace \
		-f "$(PE_VALUES_FILE)"

deploy-eyrie-agent:
	@if [ ! -f "$(PE_AGENT_VALUES_FILE)" ]; then \
		echo "ERROR: agent values file not found: $(PE_AGENT_VALUES_FILE)"; \
		exit 1; \
	fi
	helm upgrade --install "$(PE_AGENT_RELEASE)" charts/puppet-agent \
		--namespace "$(PE_NAMESPACE)" \
		--create-namespace \
		-f "$(PE_AGENT_VALUES_FILE)"

deploy-eyrie: deploy-eyrie-pe deploy-eyrie-agent

lint:
	@helm lint charts/puppet-enterprise
	@helm lint charts/puppet-agent
