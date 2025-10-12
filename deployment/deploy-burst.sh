#!/bin/bash
# Deploy and execute Cloud Run Job for burst workers

set -e

# Load configuration from .env if exists
if [ -f ../.env ]; then
    source ../.env
fi

# Configuration
PROJECT_ID=${GCP_PROJECT_ID:-"your-project-id"}
REGION=${GCP_REGION:-"us-central1"}
TEMPORAL_HOST=${COMPUTE_ENGINE_EXTERNAL_IP:-""}
IMAGE_NAME="temporal-worker"
JOB_NAME="temporal-worker-burst"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}🚀 Deploying Temporal Burst Workers to Cloud Run Jobs${NC}"

# Validate configuration
if [ -z "$TEMPORAL_HOST" ]; then
    echo -e "${YELLOW}⚠️  COMPUTE_ENGINE_EXTERNAL_IP not set in .env${NC}"
    echo -n "Enter Temporal Server IP (e.g., 34.123.45.67:7233): "
    read TEMPORAL_HOST
fi

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "your-project-id" ]; then
    echo -e "${YELLOW}⚠️  GCP_PROJECT_ID not set in .env${NC}"
    echo -n "Enter your GCP Project ID: "
    read PROJECT_ID
fi

echo -e "${BLUE}Project: ${PROJECT_ID}${NC}"
echo -e "${BLUE}Temporal Host: ${TEMPORAL_HOST}${NC}"
echo -e "${BLUE}Region: ${REGION}${NC}"

# Step 1: Build and push Docker image
echo -e "${BLUE}📦 Building Docker image...${NC}"
cd ..
docker build -f deployment/Dockerfile.worker -t gcr.io/${PROJECT_ID}/${IMAGE_NAME}:latest .

echo -e "${BLUE}🔼 Pushing to Container Registry...${NC}"
docker push gcr.io/${PROJECT_ID}/${IMAGE_NAME}:latest

# Step 2: Deploy Cloud Run Job
echo -e "${BLUE}☁️  Deploying Cloud Run Job...${NC}"
gcloud run jobs deploy ${JOB_NAME} \
  --image gcr.io/${PROJECT_ID}/${IMAGE_NAME}:latest \
  --region ${REGION} \
  --project ${PROJECT_ID} \
  --max-retries 3 \
  --task-timeout 3600 \
  --set-env-vars "WORKER_MODE=burst,TEMPORAL_HOST=${TEMPORAL_HOST},TEMPORAL_NAMESPACE=default,TEMPORAL_TASK_QUEUE=default-task-queue" \
  --cpu 2 \
  --memory 2Gi

echo -e "${GREEN}✅ Cloud Run Job deployed successfully!${NC}"
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}🚀 Execute Burst Workers:${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${YELLOW}# Run 10 workers${NC}"
echo -e "  gcloud run jobs execute ${JOB_NAME} --region ${REGION} --project ${PROJECT_ID} --tasks 10"
echo ""
echo -e "  ${YELLOW}# Run 50 workers (for large batches)${NC}"
echo -e "  gcloud run jobs execute ${JOB_NAME} --region ${REGION} --project ${PROJECT_ID} --tasks 50"
echo ""
echo -e "  ${YELLOW}# Run 100 workers (maximum burst)${NC}"
echo -e "  gcloud run jobs execute ${JOB_NAME} --region ${REGION} --project ${PROJECT_ID} --tasks 100"
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}📊 Monitor Jobs:${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  gcloud run jobs describe ${JOB_NAME} --region ${REGION} --project ${PROJECT_ID}"
echo -e "  gcloud run jobs executions list --job ${JOB_NAME} --region ${REGION} --project ${PROJECT_ID}"
echo ""
