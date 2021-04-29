# pacman-tanzu
Pac-Man the classic arcade game - deployment files for VMware Tanzu Kubernetes

# Pre-Reqs

ServiceType: LoadBalancer must be available for external connectivity to the Pac-Man front-end, otherwise you'll need to make some changes. 

# Install

Clone repo and run the "pacman-install.sh" file or the following steps

> kubectl create namespace pacman
> kubectl create -f pacman-tanzu/


# Source
These are modified files from the below github repo for the node.js version, which contain the necessary changes to run in VMware Tanzu Kubernetes Grid (TKG) such as updated api values and pod security policies (psp) with associated service accounts and RBAC.

https://github.com/font/k8s-example-apps/tree/master/pacman-nodejs-app
