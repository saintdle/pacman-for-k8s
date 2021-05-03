# Running Pac-Man on Kubernetes 
Pac-Man the classic arcade game - deployment files for Kubernetes that will be used by ArgoCD or manually using kubectl commands. 

This repository will also enable us to hook into Kasten K10 to perform a backup at every sync stage or update within ArgoCD, this will ensure that with any code changes the important data located within the MongoDB will be captured so that you have a copy of the complete namespace if there was any mistakes made. 

This requires Kasten to be deployed into your Kubernetes cluster also. 

I will add further steps to configure this with ArgoCD in a later commit. 

# Pre-Reqs

ServiceType: LoadBalancer must be available for external connectivity to the Pac-Man front-end, otherwise you'll need to make some changes. 

# Install
if you are not looking to perform CI/CD pipelines or demos around this then you can use the yaml files to complete this. 
Clone repo and run the "pacman-install.sh" file or the following steps

> kubectl create namespace pacman
> 
> kubectl create -f pacman-tanzu/

# Source
These are modified files from the below github repo for the node.js version, which contain the necessary changes to run in VMware Tanzu Kubernetes Grid (TKG) such as updated api values and pod security policies (psp) with associated service accounts and RBAC.

https://github.com/font/k8s-example-apps/tree/master/pacman-nodejs-app

Security changes to the deployment such as setting up mongodb auth were thanks to [Dav1x](https://github.com/dav1x/) you can find his [Pac-Man deployment for OpenShift here](https://github.com/dav1x/pacman-ocp). 
