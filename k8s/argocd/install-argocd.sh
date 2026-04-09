#!/bin/bash
set -e

echo "=== Installing ArgoCD into cluster ==="
kubectl create namespace argocd || true
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

echo "=== Applying custom Service Loopback and Password Patches ==="
kubectl apply -f k8s/argocd/argocd-patches.yaml

echo "=== Waiting for ArgoCD Server to boot ==="
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=300s

echo "=== ArgoCD is ready! ==="
echo "You can access ArgoCD via LoadBalancer IP."
echo "Default Username: admin"
echo "Default Password: P@\$\$s0rd"

echo "=== Deploying Trading App via GitOps ==="
kubectl apply -f k8s/argocd/trading-app.yaml
