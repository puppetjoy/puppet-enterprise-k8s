.PHONY: help build-k8s-runtime build-k8s-agent lint

CONTAINER_ENGINE ?= podman
PE_VERSION ?=
PE_INSTALLER_TAR_PATH ?=
K8S_RUNTIME_IMAGE_NAME ?= pe-k8s-runtime
K8S_RUNTIME_IMAGE_VERSION ?= $(PE_VERSION)
K8S_INSTALLER_CONTEXT_PATH ?= image/assets/pe-installer/installer.tar.gz
K8S_AGENT_IMAGE_NAME ?= pe-k8s-agent
K8S_AGENT_IMAGE_VERSION ?= dev

help:
	@echo "Puppet Enterprise on Kubernetes"
	@echo ""
	@echo "Build targets:"
	@echo "  make build-k8s-runtime PE_VERSION=<version> PE_INSTALLER_TAR_PATH=<absolute/path/to/installer.tar.gz>"
	@echo "  make build-k8s-agent K8S_AGENT_IMAGE_NAME=<image> K8S_AGENT_IMAGE_VERSION=<version>"
	@echo ""
	@echo "Validation:"
	@echo "  make lint"

build-k8s-runtime:
	@if [ -z "$(CONTAINER_ENGINE)" ]; then \
		echo "ERROR: CONTAINER_ENGINE is required"; \
		exit 1; \
	fi
	@if [ -z "$(PE_VERSION)" ] || [ -z "$(PE_INSTALLER_TAR_PATH)" ]; then \
		echo "ERROR: PE_VERSION and PE_INSTALLER_TAR_PATH are required"; \
		echo "Usage: make build-k8s-runtime PE_VERSION=<version> PE_INSTALLER_TAR_PATH=<path>"; \
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

lint:
	@helm lint charts/puppet-enterprise
	@helm lint charts/puppet-agent
