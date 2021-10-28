#!/bin/bash

kubectl delete -n pacman -f security/rbac.yaml
kubectl delete -n pacman -f security/secret.yaml
kubectl delete -n pacman -f deployments/mongo-deployment.yaml
kubectl delete -n pacman -f deployments/pacman-deployment.yaml
kubectl delete -n pacman -f services/mongo-service.yaml
kubectl delete -n pacman -f services/pacman-service.yaml

if [[ $# -gt 0  && "$1" == "keeppvc" ]]
then
    echo "Keeping namespace and persistent volume claim"
else
    kubectl delete -n pacman -f persistentvolumeclaim/mongo-pvc.yaml
    kubectl delete namespace pacman
fi