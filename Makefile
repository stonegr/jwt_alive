UV ?= uv
COMPOSE := docker compose
CONTAINER ?= jwt-alive

.DEFAULT_GOAL := help

.PHONY: help
help: ## 显示所有命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ===== 依赖 =====
.PHONY: install
install: ## 同步依赖 (uv sync)
	$(UV) sync

# ===== 开发 =====
.PHONY: init
init: ## 生成配置模板 (config/sites.yaml，不存在时才创建)
	@if [ -e config/sites.yaml ] || [ -L config/sites.yaml ]; then \
		echo "config/sites.yaml 已存在，跳过"; \
	else \
		cp config/sites.example.yaml config/sites.yaml && echo "已生成 config/sites.yaml"; \
	fi

.PHONY: dev
dev: ## 本地开发启动保活守护进程 (DEBUG 日志)
	$(UV) run jwt-alive --log-level DEBUG run

.PHONY: start
start: ## 生产启动保活守护进程 (INFO 日志)
	$(UV) run jwt-alive run

.PHONY: check-site
check-site: ## 检查所有站点登录态
	$(UV) run jwt-alive check

.PHONY: status
status: ## 查看本地状态
	$(UV) run jwt-alive status

.PHONY: validate-config
validate-config: ## 校验配置文件
	$(UV) run jwt-alive validate-config

# ===== 质量 =====
.PHONY: test
test: ## 运行 Pytest 单元测试
	$(UV) run pytest -v

.PHONY: lint
lint: ## 代码检查 (Ruff)
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: fmt
fmt: ## 代码自动格式化 (Ruff)
	$(UV) run ruff format .
	$(UV) run ruff check . --fix --unsafe-fixes

.PHONY: check
check: lint test ## 全量检查 (lint + test)

# ===== 清理 =====
.PHONY: clean
clean: ## 清理缓存与构建产物
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf htmlcov/ .coverage dist/ build/ *.egg-info

.PHONY: clean-data
clean-data: ## 清理运行期数据 (state/ 与 logs/，会丢失本地 token)
	rm -rf state/ logs/

# ===== Docker =====
.PHONY: docker-build
docker-build: ## 构建所有服务的镜像
	$(COMPOSE) build

.PHONY: docker-build-cn
docker-build-cn: ## 国内构建镜像（清华 PyPI 源 + 清华 Debian 源）
	$(COMPOSE) build \
		--build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn \
		--build-arg UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

.PHONY: docker-up
docker-up: ## 后台启动 docker compose 栈
	$(COMPOSE) up -d

.PHONY: docker-down
docker-down: ## 停止并移除容器、网络（保留数据卷和镜像）
	$(COMPOSE) down

.PHONY: docker-logs
docker-logs: ## 查看日志（默认所有服务; make docker-logs s=jwt-alive 查看指定服务）
	@if [ -n "$(s)" ]; then \
		$(COMPOSE) logs -f $(s); \
	else \
		$(COMPOSE) logs -f; \
	fi

.PHONY: docker-clean
docker-clean: ## 彻底清理：容器、网络、数据卷
	$(COMPOSE) down -v --remove-orphans

.PHONY: docker-health
docker-health: ## 查看容器健康检查状态
	docker inspect --format '{{.State.Health.Status}}' $(CONTAINER)

.PHONY: docker-check
docker-check: ## 在容器内检查所有站点登录态
	$(COMPOSE) exec jwt-alive jwt-alive check

.PHONY: docker-shell
docker-shell: ## 进入容器调试
	$(COMPOSE) exec jwt-alive /bin/bash

APP_NAME?=jwt-alive
PY_FILE?=./jwt_alive/cli.py
BUILD_DIR?=dist
# 默认使用的 bullseye 只支持4种架构
PYTHON_VERSION ?= '3.12'
DEBIAN_VERSION ?= 'trixie'
ALPINE_VERSION ?= '3.24'
IMAGE_PREFIX := $(shell basename $$(pwd))
ARCH_NAME = $(shell echo $(ARCH) | tr '/' '-')

.PHONY: pyinstaller-check
pyinstaller-check:
ifeq ($(OS),Windows_NT)
	@echo "🪟 Windows 环境"
	@powershell -Command " \
		if (Get-Command uv -ErrorAction SilentlyContinue) { \
			if (-not (uv pip list | Select-String pyinstaller)) { \
				uv add --group dev pyinstaller \
			} \
		} else { \
			pip show pyinstaller >nul 2>nul; \
			if ($$LASTEXITCODE -ne 0) { \
				echo 'pyinstaller 未安装'; exit 1 \
			} \
		}"
else
	@set -e; \
	echo "🐧 Linux/macOS 环境"; \
	if command -v uv >/dev/null 2>&1; then \
		if ! uv pip list | grep -q pyinstaller; then \
			echo "uv 环境未安装 pyinstaller，开始添加 dev group"; \
			uv add --group dev pyinstaller; \
		fi \
	else \
		if ! pip show pyinstaller >/dev/null 2>&1; then \
			echo "pip 环境未安装 pyinstaller"; \
			exit 1; \
		fi \
	fi
endif

.PHONY: docker-buildx-check
docker-buildx-check:
	@if docker buildx inspect orbstack >/dev/null 2>&1; then \
		echo "Using OrbStack builder..."; \
		docker buildx use orbstack; \
	else \
		echo "OrbStack not found, checking current builder..."; \
		if docker buildx inspect default >/dev/null 2>&1 && \
		   docker buildx inspect default | grep -q "linux/arm64"; then \
			echo "Current builder supports linux/arm64."; \
			docker buildx use default; \
		else \
			echo "No arm64 support found. Installing QEMU..."; \
			docker run --privileged --rm tonistiigi/binfmt:latest --install all; \
		fi; \
	fi

.PHONY: binary
binary: pyinstaller-check ## 本地打包可执行文件
ifeq ($(OS),Windows_NT)
	@echo "🪟 Windows 环境"

	@if command -v uv >nul 2>nul; then \
		echo "🚀 uv run 模式"; \
		uv run pyinstaller --onefile --strip --distpath $(BUILD_DIR) --name $(APP_NAME).exe $(PY_FILE); \
	else \
		pyinstaller --onefile --strip --distpath $(BUILD_DIR) --name $(APP_NAME).exe $(PY_FILE); \
		fi; \

	rmdir /s /q build 2>nul || exit 0
	del /f /q $(APP_NAME).spec 2>nul || exit 0
	echo "✅ 输出: $(APP_NAME).exe"

else
	@set -e; \
	if command -v uv >/dev/null 2>&1; then \
		echo "🚀 uv run 模式"; \
		uv run pyinstaller --onefile --strip --distpath $(BUILD_DIR) --name $(APP_NAME) $(PY_FILE); \
	else \
		echo "📦 直接 pyinstaller"; \
		pyinstaller --onefile --strip --distpath $(BUILD_DIR) --name $(APP_NAME) $(PY_FILE); \
	fi; \
	rm -rf build/ $(APP_NAME).spec; \
	echo "✅ 二进制已输出: $(APP_NAME)"
endif

# ================================
# 多平台打包
# ================================

.PHONY: build-all
build-all: ## 一键打包所有平台 (linux/windows/darwin × amd64/arm64)
	@echo "Building $(APP_NAME) for all platforms..."
	@mkdir -p $(BUILD_DIR)
	@$(MAKE) binary-cross ARCH=amd64
	@$(MAKE) binary-cross ARCH=arm64
	@$(MAKE) binary-cross ARCH=riscv64
	@$(MAKE) binary-cross ARCH=ppc64le
	@$(MAKE) binary-cross ARCH=s390x
	@$(MAKE) binary-cross ARCH=386
	@$(MAKE) binary-cross ARCH=arm/v7
	@$(MAKE) binary-cross ARCH=arm/v6
	# musl
	@$(MAKE) binary-cross-musl ARCH=amd64
	@$(MAKE) binary-cross-musl ARCH=arm64
	@$(MAKE) binary-cross-musl ARCH=riscv64
	@$(MAKE) binary-cross-musl ARCH=ppc64le
	@$(MAKE) binary-cross-musl ARCH=s390x
	@$(MAKE) binary-cross-musl ARCH=386
	@$(MAKE) binary-cross-musl ARCH=arm/v7
	@$(MAKE) binary-cross-musl ARCH=arm/v6
	@echo ""
	@echo "All builds complete:"
	@ls -lh $(BUILD_DIR)/

.PHONY: build-image
build-image: docker-buildx-check ## ARCH=amd64 [FORCE=1] [CN=1] 构建预安装依赖的 Docker 镜像
	@if [ "$(FORCE)" = "1" ]; then \
		echo "强制重建镜像 $(IMAGE_PREFIX)-$(ARCH_NAME)..."; \
		docker rmi $(IMAGE_PREFIX)-$(ARCH_NAME) 2>/dev/null || true; \
	fi
	@echo "FROM python:$(PYTHON_VERSION)-slim-$(DEBIAN_VERSION)\n \
	WORKDIR /app\n \
	$(shell [ -f requirements.txt ] && echo 'COPY requirements.txt .' || true)\n \
	RUN $(if $(filter 1,$(CN)),find /etc/apt/ -type f \( -name '*.list' -o -name '*.sources' \) -exec sed -i -e 's@deb.debian.org@mirrors.tuna.tsinghua.edu.cn@g' -e 's@security.debian.org@mirrors.tuna.tsinghua.edu.cn@g' {} + &&) \
	apt-get update -y && apt-get install -y gcc g++ make libc6-dev python3-dev && apt-get clean && rm -rf /var/lib/apt/lists/*\n \
	RUN pip install pyinstaller --break-system-packages $(shell [ -f requirements.txt ] && echo '-r requirements.txt' || true) $(shell [ $(CN) = '1' ] && echo '-i https://pypi.tuna.tsinghua.edu.cn/simple' || true)" \
	| docker buildx build -t $(IMAGE_PREFIX)-$(ARCH_NAME) \
	--platform linux/$(ARCH) \
	--load -f - .
	@echo "✅ 镜像已构建: $(IMAGE_PREFIX)-$(ARCH_NAME)"

.PHONY: build-image-musl
build-image-musl: docker-buildx-check ## ARCH=amd64 [FORCE=1] [CN=1] 构建 musl 预安装依赖的 Docker 镜像
	@if [ "$(FORCE)" = "1" ]; then \
		echo "强制重建镜像 $(IMAGE_PREFIX)-$(ARCH_NAME)-musl..."; \
		docker rmi $(IMAGE_PREFIX)-$(ARCH_NAME)-musl 2>/dev/null || true; \
	fi
	@echo "FROM python:$(PYTHON_VERSION)-alpine$(ALPINE_VERSION)\n \
	WORKDIR /app\n \
	$(shell [ -f requirements.txt ] && echo 'COPY requirements.txt .' || true)\n \
	RUN $(if $(filter 1,$(CN)),sed -i 's/dl-cdn.alpinelinux.org/mirrors.ustc.edu.cn/g' /etc/apk/repositories &&) \
	apk add --no-cache gcc g++ make musl-dev python3-dev py3-pip zlib-dev build-base\n \
	RUN pip install pyinstaller --break-system-packages $(shell [ -f requirements.txt ] && echo '-r requirements.txt' || true) $(shell [ $(CN) = '1' ] && echo '-i https://pypi.tuna.tsinghua.edu.cn/simple' || true)" \
	| docker buildx build -t $(IMAGE_PREFIX)-$(ARCH_NAME)-musl \
	--platform linux/$(ARCH) \
	--load -f - .
	@echo "✅ 镜像已构建: $(IMAGE_PREFIX)-$(ARCH_NAME)-musl"

.PHONY: binary-cross
binary-cross: ## ARCH=arm64 amd64 riscv64 ppc64le s390x 386 mips64le arm/v7 arm/v6 [CN=1]
	@mkdir -p $(BUILD_DIR)
	@IMAGE_NAME=$(IMAGE_PREFIX)-$(ARCH_NAME) && \
	if ! docker image inspect $${IMAGE_NAME} >/dev/null 2>&1; then \
		echo "镜像 $${IMAGE_NAME} 不存在，正在构建..."; \
		$(MAKE) build-image ARCH=$(ARCH) CN=$(CN); \
	fi
	@docker run --rm -v "$$(pwd):/dist" \
		-w /dist \
		--platform linux/$(ARCH) \
		$(IMAGE_PREFIX)-$(ARCH_NAME) \
		sh -c "\
			BINARY_NAME=$(APP_NAME)-$(ARCH_NAME) && \
			pyinstaller --onefile --strip --distpath $(BUILD_DIR) --name \$${BINARY_NAME} $(PY_FILE) && \
			echo '✅ 二进制已输出: '\$${BINARY_NAME} \
		"
	@rm -rf build/ $(APP_NAME)-$(ARCH_NAME).spec

.PHONY: binary-cross-musl
binary-cross-musl: ## ARCH=arm64 amd64 riscv64 ppc64le s390x 386 mips64le arm/v7 arm/v6 [CN=1]
	@mkdir -p $(BUILD_DIR)
	@IMAGE_NAME=$(IMAGE_PREFIX)-$(ARCH_NAME)-musl && \
	if ! docker image inspect $${IMAGE_NAME} >/dev/null 2>&1; then \
		echo "镜像 $${IMAGE_NAME} 不存在，正在构建..."; \
		$(MAKE) build-image-musl ARCH=$(ARCH) CN=$(CN); \
	fi
	@docker run --rm -v "$$(pwd):/dist" \
		-w /dist \
		--platform linux/$(ARCH) \
		$(IMAGE_PREFIX)-$(ARCH_NAME)-musl \
		sh -c "\
			BINARY_NAME=$(APP_NAME)-$(ARCH_NAME)-musl && \
			pyinstaller --onefile --strip --distpath $(BUILD_DIR) --name \$${BINARY_NAME} $(PY_FILE) && \
			echo '✅ 二进制已输出: '\$${BINARY_NAME} \
		"
	@rm -rf build/ $(APP_NAME)-$(ARCH)-musl.spec

.PHONY: clean-image
clean-image: ## 清理所有编译相关镜像
	@echo "清理 $(IMAGE_PREFIX)-* 镜像..."
	@docker images --format '{{.Repository}}:{{.Tag}}' | grep '^$(IMAGE_PREFIX)-' | xargs -r docker rmi 2>/dev/null || true
	@echo "✅ 镜像已清理"

.PHONY: clean-binary
clean-binary: ## 清理构建产物
	@rm -rf $(BUILD_DIR) build/ *.spec
	@echo "Cleaned $(BUILD_DIR)"