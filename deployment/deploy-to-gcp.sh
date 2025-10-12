#!/bin/bash
# Deploy application code to GCP Compute Engine instance (ai-core-instance)

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Load configuration from .env if exists
if [ -f ../.env ]; then
    source ../.env
fi

# Configuration
INSTANCE_NAME=${GCP_INSTANCE_NAME:-"ai-core-instance"}
ZONE=${GCP_ZONE:-"us-central1-a"}
PROJECT_ID=${GCP_PROJECT_ID:-""}

if [ -z "$PROJECT_ID" ]; then
    echo -e "${YELLOW}⚠️  GCP_PROJECT_ID not set in .env file${NC}"
    echo -n "Enter your GCP Project ID: "
    read PROJECT_ID
fi

echo -e "${BLUE}🚀 Deploying to GCP Compute Engine${NC}"
echo -e "${BLUE}Instance: ${INSTANCE_NAME}${NC}"
echo -e "${BLUE}Zone: ${ZONE}${NC}"
echo -e "${BLUE}Project: ${PROJECT_ID}${NC}"

# Check if instance exists
echo -e "${BLUE}🔍 Checking if instance exists...${NC}"
if ! gcloud compute instances describe ${INSTANCE_NAME} --zone=${ZONE} --project=${PROJECT_ID} > /dev/null 2>&1; then
    echo -e "${YELLOW}❌ Instance ${INSTANCE_NAME} not found${NC}"
    echo -e "${YELLOW}Creating instance...${NC}"

    gcloud compute instances create ${INSTANCE_NAME} \
        --project=${PROJECT_ID} \
        --zone=${ZONE} \
        --machine-type=e2-medium \
        --boot-disk-size=30GB \
        --boot-disk-type=pd-balanced \
        --image-family=ubuntu-2204-lts \
        --image-project=ubuntu-os-cloud \
        --tags=temporal-server \
        --metadata=enable-oslogin=TRUE

    echo -e "${GREEN}✅ Instance created${NC}"
    echo -e "${YELLOW}⏳ Waiting for instance to be ready...${NC}"
    sleep 30
else
    echo -e "${GREEN}✅ Instance exists${NC}"
fi

# Upload application code
echo -e "${BLUE}📦 Uploading application code...${NC}"
cd ..
gcloud compute scp --recurse \
    --zone=${ZONE} \
    --project=${PROJECT_ID} \
    app/ \
    temporal_app/ \
    deployment/ \
    pyproject.toml \
    .python-version \
    ${INSTANCE_NAME}:~/temporal-deployment/

echo -e "${GREEN}✅ Code uploaded${NC}"

# Run setup script on instance
echo -e "${BLUE}🔧 Running setup script on instance...${NC}"
gcloud compute ssh ${INSTANCE_NAME} \
    --zone=${ZONE} \
    --project=${PROJECT_ID} \
    --command="cd ~/temporal-deployment/deployment && chmod +x setup-compute-engine.sh && ./setup-compute-engine.sh"

echo -e "${GREEN}✅✅✅ Deployment complete! ✅✅✅${NC}"
