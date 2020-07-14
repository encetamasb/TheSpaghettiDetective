FROM thespaghettidetective/web:base-1.1

ARG TSD_UID=0
ARG TSD_GID=0
ARG TSD_USER=root
ARG TSD_GROUP=root
ARG WITH_NODE=0
ARG WITH_SQLITE=0

RUN if [[ "${WITH_NODE}" == 1 ]]; then \
    apk add --no-cache --update inotify-tools nodejs npm && npm install -g yarn \
    ; fi && \
    if [[ "${WITH_SQLITE}" == 1 ]]; then \
    apk add --no-cache sqlite \
    ; fi

RUN echo "User/group configuration: UID: ${TSD_UID} | GID: ${TSD_GID} | USER: ${TSD_USER} | GROUP: ${TSD_GROUP}" && \
    if [[ "${TSD_UID}" != 0 ]]; then \
    (addgroup --gid ${TSD_GID} ${TSD_GROUP} || echo "group with gid ${TSD_GID} exists") && \
    adduser --disabled-password --uid ${TSD_UID} --ingroup $(cat /etc/group|grep ":${TSD_GID}:" |cut -d: -f1) ${TSD_USER} && \
    mkdir -p /home/${TSD_USER} /home/${TSD_USER}/.cache /home/${TSD_USER}/.ipython /home/${TSD_USER}/.local && \
    mkdir -p /app/ /app/frontend /app/static_build && \
    chown -R ${TSD_UID}:${TSD_GID} /app /home/${TSD_USER} \
    ; fi

USER ${TSD_USER}

EXPOSE 3334
ADD . /app
RUN if [[ "${TSD_UID}" != 0 ]]; then \
    pip install -U pip && pip install -r requirements.txt --src /home/${TSD_USER}/.local/src \
    ; else \
    pip install -U pip && pip install -r requirements.txt \
    ; fi


VOLUME /app
VOLUME /home/${TSD_USER}/.cache
VOLUME /home/${TSD_USER}/.ipython
VOLUME /home/${TSD_USER}/.local

WORKDIR /app

ENV PYTHONPATH "/home/${TSD_USER}/.local:${PYTHONPATH}"
ENV PATH "/home/${TSD_USER}/.local/bin:${PATH}"
