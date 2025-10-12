#!/bin/bash
# Setup script for Compute Engine (ai-core-instance)
# This script deploys Temporal + Base Workers + FastAPI

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}🚀 Setting up Temporal on Compute Engine (ai-core-instance)${NC}"

# Check if running on Compute Engine
if ! curl -s -f -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/name > /dev/null 2>&1; then
    echo -e "${RED}❌ This script must be run on a GCP Compute Engine instance${NC}"
    exit 1
fi

# Get instance metadata
INSTANCE_NAME=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/name)
ZONE=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone | cut -d'/' -f4)
PROJECT_ID=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/project/project-id)

echo -e "${BLUE}Instance: ${INSTANCE_NAME}${NC}"
echo -e "${BLUE}Zone: ${ZONE}${NC}"
echo -e "${BLUE}Project: ${PROJECT_ID}${NC}"

# Update system
echo -e "${BLUE}📦 Updating system...${NC}"
sudo apt-get update -qq

# Install Docker if not present
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}📦 Installing Docker...${NC}"
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    rm get-docker.sh
    echo -e "${GREEN}✅ Docker installed${NC}"
else
    echo -e "${GREEN}✅ Docker already installed${NC}"
fi

# Install Docker Compose if not present
if ! command -v docker-compose &> /dev/null; then
    echo -e "${YELLOW}📦 Installing Docker Compose...${NC}"
    sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
    echo -e "${GREEN}✅ Docker Compose installed${NC}"
else
    echo -e "${GREEN}✅ Docker Compose already installed${NC}"
fi

# Create deployment directory
DEPLOY_DIR="$HOME/temporal-deployment"
echo -e "${BLUE}📁 Creating deployment directory: ${DEPLOY_DIR}${NC}"
mkdir -p ${DEPLOY_DIR}

# Create .env file
echo -e "${BLUE}📝 Creating .env file...${NC}"
cat > ${DEPLOY_DIR}/.env << EOF
# Temporal Configuration
TEMPORAL_HOST=temporal:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_TASK_QUEUE=default-task-queue
TEMPORAL_USE_CLOUD=false

# Worker Configuration
WORKER_MODE=base

# Application
APP_NAME=Temporal Task System
APP_VERSION=0.1.0
DEBUG=false

# GCP Configuration
GCP_PROJECT_ID=${PROJECT_ID}
GCP_REGION=us-central1
GCP_INSTANCE_NAME=${INSTANCE_NAME}
GCP_ZONE=${ZONE}
EOF

echo -e "${GREEN}✅ .env file created${NC}"

# Note: Application code should be deployed separately (via git clone or gcloud scp)
echo ""
echo -e "${YELLOW}⚠️  Please ensure your application code is deployed to ${DEPLOY_DIR}${NC}"
echo -e "${YELLOW}   You can:${NC}"
echo -e "${YELLOW}   1. Clone from git: cd ${DEPLOY_DIR} && git clone <your-repo> .${NC}"
echo -e "${YELLOW}   2. Or upload files: gcloud compute scp --recurse ./* ${INSTANCE_NAME}:${DEPLOY_DIR}/ --zone=${ZONE}${NC}"
echo ""
echo -e "${BLUE}Press Enter when code is deployed...${NC}"
read

# Check if docker-compose.yml exists
if [ ! -f "${DEPLOY_DIR}/deployment/docker-compose.yml" ]; then
    echo -e "${RED}❌ docker-compose.yml not found in ${DEPLOY_DIR}/deployment/${NC}"
    echo -e "${YELLOW}Please ensure application code is deployed${NC}"
    exit 1
fi

# Start services
cd ${DEPLOY_DIR}
echo -e "${BLUE}🚀 Starting Temporal services with Docker Compose...${NC}"
docker-compose -f deployment/docker-compose.yml up -d

# Wait for services to be healthy
echo -e "${BLUE}⏳ Waiting for services to start (this may take 30-60 seconds)...${NC}"
sleep 20

# Check service status
echo -e "${BLUE}📊 Service status:${NC}"
docker-compose -f deployment/docker-compose.yml ps

# Get external IP
EXTERNAL_IP=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)

# Update .env with external IP
echo "COMPUTE_ENGINE_EXTERNAL_IP=${EXTERNAL_IP}" >> ${DEPLOY_DIR}/.env

# Configure firewall (if not already done)
echo ""
echo -e "${BLUE}🔥 Configuring firewall rules...${NC}"
gcloud compute firewall-rules create temporal-server \
    --project=${PROJECT_ID} \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=tcp:7233,tcp:8080,tcp:8000 \
    --source-ranges=0.0.0.0/0 \
    --target-tags=temporal-server 2>/dev/null || echo -e "${YELLOW}Firewall rule already exists${NC}"

# Add network tag to instance
gcloud compute instances add-tags ${INSTANCE_NAME} \
    --project=${PROJECT_ID} \
    --zone=${ZONE} \
    --tags=temporal-server 2>/dev/null || echo -e "${YELLOW}Tag already applied${NC}"

# Test services
echo ""
echo -e "${BLUE}🧪 Testing services...${NC}"
sleep 5

# Test FastAPI
if curl -s -f http://localhost:8000/ > /dev/null; then
    echo -e "${GREEN}✅ FastAPI is running${NC}"
else
    echo -e "${RED}❌ FastAPI is not responding${NC}"
fi

# Test Temporal UI
if curl -s -f http://localhost:8080/ > /dev/null; then
    echo -e "${GREEN}✅ Temporal Web UI is running${NC}"
else
    echo -e "${RED}❌ Temporal Web UI is not responding${NC}"
fi

echo ""
echo -e "${GREEN}✅✅✅ Temporal setup complete! ✅✅✅${NC}"
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}📍 Access Points:${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  🌐 Temporal Server:  ${GREEN}${EXTERNAL_IP}:7233${NC}"
echo -e "  🖥️  Temporal Web UI:  ${GREEN}http://${EXTERNAL_IP}:8080${NC}"
echo -e "  🚀 FastAPI:          ${GREEN}http://${EXTERNAL_IP}:8000${NC}"
echo -e "  📊 API Docs:         ${GREEN}http://${EXTERNAL_IP}:8000/docs${NC}"
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}🔧 Useful Commands:${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  View logs:           ${YELLOW}cd ${DEPLOY_DIR} && docker-compose -f deployment/docker-compose.yml logs -f${NC}"
echo -e "  View worker logs:    ${YELLOW}docker-compose -f deployment/docker-compose.yml logs -f temporal-worker${NC}"
echo -e "  Restart services:    ${YELLOW}docker-compose -f deployment/docker-compose.yml restart${NC}"
echo -e "  Stop services:       ${YELLOW}docker-compose -f deployment/docker-compose.yml down${NC}"
echo -e "  Scale workers:       ${YELLOW}docker-compose -f deployment/docker-compose.yml up -d --scale temporal-worker=5${NC}"
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}🧪 Test the API:${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━��━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${YELLOW}curl http://${EXTERNAL_IP}:8000/${NC}"
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}🚀 Next Step: Deploy Cloud Run Burst Workers${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  Update ${YELLOW}deployment/deploy-burst.sh${NC} with:"
echo -e "    ${GREEN}TEMPORAL_HOST=${EXTERNAL_IP}:7233${NC}"
echo ""
echo -e "  Then run: ${YELLOW}cd deployment && ./deploy-burst.sh${NC}"
echo ""
