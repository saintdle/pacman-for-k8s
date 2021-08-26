## Running Pac-Man on Tanzu Kubernetes Grid Clusters
Pac-Man the classic arcade game - deployment files for VMware Tanzu Kubernetes

## Pre-Reqs

ServiceType: LoadBalancer must be available for external connectivity to the Pac-Man front-end, otherwise you'll need to make some changes to the files in the "services" folder.

## Install

Clone repo and run ```chmod +X pacman-install.sh``` and then run file ```./pacman-install.sh```

or the following steps:

````
kubectl create namespace pacman
kubectl create -n pacman -f pacman-tanzu/
````

## Source
These are modified files from the below github repo for the node.js version, which contain the necessary changes to run in VMware Tanzu Kubernetes Grid (TKG) such as updated api values and pod security policies (psp) with associated service accounts and RBAC.

> https://github.com/font/k8s-example-apps/tree/master/pacman-nodejs-app

Security changes to the deployment such as setting up mongodb auth were thanks to [Dav1x](https://github.com/dav1x/) you can find his [Pac-Man deployment for OpenShift here](https://github.com/dav1x/pacman-ocp). 
