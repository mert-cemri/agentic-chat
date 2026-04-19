#!/bin/bash
#
# GCP deployment script for agentic-chat
# Paste this entire script into GCP Cloud Shell.
#
# What it does:
#   1. Creates a small VM (e2-small, ~$15/mo)
#   2. Opens port 4444 for the relay
#   3. Installs Python, the relay, and starts it as a systemd service
#   4. Creates your first token
#   5. Prints the dashboard URL and connection commands
#
# Prerequisites: a GCP project with billing enabled.
#

set -e

PROJECT=$(gcloud config get-value project 2>/dev/null)
ZONE="us-central1-a"
VM_NAME="agentic-chat"

echo ""
echo "========================================="
echo "  Agentic Chat — GCP Deployment"
echo "========================================="
echo ""
echo "  Project: $PROJECT"
echo "  Zone:    $ZONE"
echo "  VM:      $VM_NAME (e2-small)"
echo ""

# ── 1. Create the VM ─────────────────────────────────
echo "[1/6] Creating VM..."
gcloud compute instances create "$VM_NAME" \
  --zone="$ZONE" \
  --machine-type=e2-small \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --tags=relay-server \
  --metadata=startup-script='#!/bin/bash
    apt-get update -qq
    apt-get install -y -qq python3 python3-pip python3-venv git
  ' \
  2>/dev/null || echo "  (VM may already exist, continuing...)"

# ── 2. Open port 4444 ───────────────────────────────
echo "[2/6] Opening firewall port 4444..."
gcloud compute firewall-rules create allow-relay-4444 \
  --allow tcp:4444 \
  --target-tags relay-server \
  --description "Allow agentic-chat relay traffic" \
  2>/dev/null || echo "  (Firewall rule may already exist, continuing...)"

# ── 3. Get the external IP ───────────────────────────
echo "[3/6] Getting external IP..."
EXTERNAL_IP=$(gcloud compute instances describe "$VM_NAME" \
  --zone="$ZONE" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
echo "  External IP: $EXTERNAL_IP"

# ── 4. Wait for VM to be ready ──────────────────────
echo "[4/6] Waiting for VM to be ready (30s)..."
sleep 30

# ── 5. Deploy the relay ─────────────────────────────
echo "[5/6] Deploying agentic-chat to VM..."
gcloud compute ssh "$VM_NAME" --zone="$ZONE" --command='
  set -e

  # Clone the repo
  if [ ! -d /opt/agentic-chat ]; then
    sudo git clone https://github.com/mert-cemri/agentic-chat /opt/agentic-chat
  else
    cd /opt/agentic-chat && sudo git pull
  fi

  cd /opt/agentic-chat

  # Create venv and install deps
  sudo python3 -m venv /opt/agentic-chat/.venv
  sudo /opt/agentic-chat/.venv/bin/pip install -q -r requirements.txt

  # Initialize if not already done
  if [ ! -f relay.config.json ]; then
    echo -e "4444\ndefault" | sudo /opt/agentic-chat/.venv/bin/python relay.py init
  fi

  # Create systemd service
  sudo tee /etc/systemd/system/agentic-chat.service > /dev/null <<SVCEOF
[Unit]
Description=Agentic Chat Relay
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/agentic-chat
ExecStart=/opt/agentic-chat/.venv/bin/python relay.py serve
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF

  sudo systemctl daemon-reload
  sudo systemctl enable agentic-chat
  sudo systemctl restart agentic-chat

  # Wait for server to start
  sleep 3

  # Create a token for the deployer
  DEPLOYER_OUTPUT=$(/opt/agentic-chat/.venv/bin/python relay.py token create --owner admin --url "http://'"$HOSTNAME"':4444" 2>&1)
  echo "$DEPLOYER_OUTPUT"

  echo ""
  echo "RELAY_READY"
'

# ── 6. Print results ────────────────────────────────
echo ""
echo "========================================="
echo "  Deployment Complete!"
echo "========================================="
echo ""
echo "  Dashboard:  http://$EXTERNAL_IP:4444/dashboard"
echo "  Health:     http://$EXTERNAL_IP:4444/health"
echo ""
echo "  To create tokens:"
echo "  gcloud compute ssh $VM_NAME --zone=$ZONE -- \\"
echo "    'cd /opt/agentic-chat && .venv/bin/python relay.py token create --owner YOUR_NAME --url http://$EXTERNAL_IP:4444'"
echo ""
echo "  To connect Claude Code (run once on your machine):"
echo "  claude mcp add -t http -s user -H \"Authorization: Bearer YOUR_TOKEN\" -- relay http://$EXTERNAL_IP:4444/mcp"
echo ""
echo "  To SSH into the VM:"
echo "  gcloud compute ssh $VM_NAME --zone=$ZONE"
echo ""
echo "  To view logs:"
echo "  gcloud compute ssh $VM_NAME --zone=$ZONE -- 'journalctl -u agentic-chat -f'"
echo ""
