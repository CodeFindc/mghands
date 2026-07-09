# Mghands

Mghands is a lightweight FastAPI gateway designed to orchestrate and manage isolated OpenHands SDK sandbox containers. 

It provides session management, project isolation, user authentication, snapshot-based skill sharing, and a modern, premium web frontend (React + TypeScript + Vite) for real-time tracking of agent events via SSE (Server-Sent Events).

---

## 🏗️ Architecture

Mghands decouples the client frontend, user management, and container scheduling from the actual OpenHands execution environment.

```text
┌──────────────────┐
│  Client Web UI   │
└────────┬─────────┘
         │ (HTTP, SSE Stream)
┌────────▼─────────┐      (Starts/Stops)      ┌───────────────────────────┐
│ FastAPI Gateway  ├─────────────────────────►│ per-session Docker Sandbox│
│ (mghands_gateway)│                          │    (mghands_sandbox)      │
└──────────────────┘                          └─────────────┬─────────────┘
                                                            │ (Internal call)
                                                      ┌─────▼─────────────┐
                                                      │  OpenHands SDK    │
                                                      │ (Conversation/Run)│
                                                      └───────────────────┘
```

- **FastAPI Gateway (`mghands_gateway`)**: Handles user authentication, token hashing, projects/workspaces storage in SQLite, and dynamic sandbox creation using sibling containers via the host Docker daemon.
- **Docker Sandbox (`mghands_sandbox`)**: A container-internal adapter that wraps the OpenHands SDK to expose stable coding tools, runs conversations, and captures raw SDK callback events into history records.
- **Web Frontend**: A single-page application built on React, TypeScript, and Vite, featuring user login, project setup, active session lifecycle controls, prompt submission, and real-time streaming timelines.

---

## 📂 Key Directories

```text
├── src/
│   ├── mghands_gateway/   # FastAPI Gateway service and Docker session scheduling
│   └── mghands_sandbox/   # Container-side OpenHands SDK adapter & API endpoints
├── web/                   # Vite + React + TypeScript web frontend
├── tests/                 # Unit tests for Gateway configurations and Sandbox runtime
├── Dockerfile             # Multi-stage production Dockerfile for Gateway & Web build
├── Dockerfile.sandbox     # Dockerfile for the isolated OpenHands sandbox image
├── docker-compose.yaml    # Docker Compose setup for standard deployments
├── RUNBOOK.md             # Operational guide with endpoints, payloads, and debug commands
└── AGENTS.md              # Reference design guide for contributors and agents
```

---

## 🚀 Quick Start

### Method 1: Deploy with Docker Compose (Recommended)

Docker Compose builds the Gateway and frontend, mounts the host Docker socket, and automatically configures sandbox containers on a shared network (`mghands`).

1. **Build the Images**
   ```bash
   # Build the main Gateway and Web UI image
   docker compose build gateway

   # Build the OpenHands Sandbox image
   docker compose --profile build-only build sandbox-image
   ```

2. **Configure Environment Variables**
   Set the administrator credentials in your shell before starting:
   ```powershell
   # Windows PowerShell
   $env:MGHANDS_BOOTSTRAP_ADMIN_USERNAME="admin"
   $env:MGHANDS_BOOTSTRAP_ADMIN_PASSWORD="change-me-strong-password"
   ```
   ```bash
   # Linux/macOS
   export MGHANDS_BOOTSTRAP_ADMIN_USERNAME="admin"
   export MGHANDS_BOOTSTRAP_ADMIN_PASSWORD="change-me-strong-password"
   ```

3. **Start the Application**
   ```bash
   docker compose up gateway
   ```
   Open `http://localhost:8080` in your browser to access the Mghands Gateway workspace.

---

### Method 2: Local Development Setup

If you want to run the services outside container networks for development:

#### Backend Gateway Setup
1. Install Python packages:
   ```bash
   pip install -e .
   ```
2. Set configuration and run the service:
   ```bash
   # By default, runs on port 8080
   python -m uvicorn mghands_gateway.app:app --reload
   ```

#### Frontend Setup
1. Navigate to the `web` directory and install node modules:
   ```bash
   cd web
   npm install
   ```
2. Build or start in development mode:
   ```bash
   # Start dev server with API proxying
   npm run dev

   # Compile for production
   npm run build
   ```

---

## 🛡️ Testing & Validation

Run the pytest suite to verify all database, session store, models, and sandbox URL resolution functions:

```bash
pytest
```

---

## 📖 Further Reading

- Refer to [RUNBOOK.md](file:///d:/iso/Mghands/RUNBOOK.md) for full descriptions of public Gateway APIs, container APIs, event structures, and deployment troubleshooting.
- Refer to [AGENTS.md](file:///d:/iso/Mghands/AGENTS.md) for details on codebase architecture, prompt execution ordering, and the timeline mapping system.
