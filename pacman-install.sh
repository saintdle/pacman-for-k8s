#!/bin/sh

kubectl create namespace pacman
kubectl create -n pacman -f security/rbac.yaml
kubectl create -n pacman -f security/secret.yaml
kubectl create -n pacman -f persistentvolumeclaim/mongo-pvc.yaml
kubectl create -n pacman -f deployments/mongo-deployment.yaml
while [ "$(kubectl get pods -l=name='mongo' -n pacman -o jsonpath='{.items[*].status.containerStatuses[0].ready}')" != "true" ]; do
   sleep 5
   echo "Waiting for mongo pod to change to running status"
done
kubectl create -n pacman -f deployments/pacman-deployment.yaml
kubectl create -n pacman -f services/mongo-service.yaml
kubectl create -n pacman -f services/pacman-service.yaml