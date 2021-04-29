#!/bin/sh

kubectl create namespace pacman
kubectl create -n pacman -f rbac/rbac.yaml
kubectl create -n pacman -f persistentvolumeclaim/mongo-pvc.yaml
kubectl create -n pacman -f deployments/mongo-deployment.yaml
kubectl create -n pacman -f deployments/pacman-deployment.yaml
kubectl create -n pacman -f services/mongo-service.yaml
kubectl create -n pacman -f services/pacman-service.yaml
