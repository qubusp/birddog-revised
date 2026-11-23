# JupyterHub (the hub process only). Users' notebook servers run in separate
# containers started by DockerSpawner from singleuser.Dockerfile.
FROM jupyterhub/jupyterhub:6.1.1

RUN python3 -m pip install --no-cache-dir \
        dockerspawner==14.0.0 \
        jupyterhub-nativeauthenticator==1.4.0 \
        jupyterhub-idle-culler==2.0.0

COPY deploy/jupyterhub/jupyterhub_config.py /etc/jupyterhub/jupyterhub_config.py
WORKDIR /data
CMD ["jupyterhub", "-f", "/etc/jupyterhub/jupyterhub_config.py"]
