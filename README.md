# healmind

#### Changes are required to run default project in (add `ease` as const):
- healmind-web/components/agents-ui/agent-control-bar.tsx
- healmind-web/components/app/chat-transcript.tsx
- healmind-web/components/app/session-view.tsx
- healmind-web/components/app/tile-layout.tsx
- healmind-web/components/app/view-controller.tsx

#### Install pm2 globally:
```
# install pm2 globally
npm i -g pm2
```
#### Launch healmind agent:
```
source .venv/bin/activate
uv sync
pm2 start .venv/bin/python --name healmind-agent --interpreter none -- src/agent.py start
```
#### Launch healmind web app:
```
pnpm install
pnpm build
pm2 start "$(command -v pnpm)" --name myapp --interpreter bash -- start
```

#### Get logs:
```
# 1) confirm the process name / id and status
pm2 ls

# 2) show full details (often includes last error + restart count)
pm2 describe healmind-web

# 3) tail logs (stdout + stderr)
pm2 logs healmind-web

# 4) tail only errors (stderr)
pm2 logs healmind-web --err

# 5) show the last N lines (useful if logs are noisy)
pm2 logs healmind-web --lines 200
```
### Git commands:

#### Change branch (new/create: -c)
```
git switch (-c) feature-branch
git pull origin feature-branch
git push -u origin feature-branch
```


