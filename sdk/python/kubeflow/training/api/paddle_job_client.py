# Copyright 2021 The Kubeflow Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import multiprocessing
import time
import logging
from typing import Callable, List, Dict, Any
from kubernetes import client, config

from kubeflow.training.constants import constants
from kubeflow.training.utils import utils
from kubeflow.training import models
from kubeflow.training.api.paddle_job_watch import watch as paddlejob_watch

logging.basicConfig(format="%(message)s")
logging.getLogger().setLevel(logging.INFO)


class PaddleJobClient(object):
    def __init__(
        self,
        config_file=None,
        context=None,  # pylint: disable=too-many-arguments
        client_configuration=None,
        persist_config=True,
    ):
        """
        PaddleJob client constructor
        :param config_file: kubeconfig file, defaults to ~/.kube/config
        :param context: Kubernetes context
        :param client_configuration: configuration for Kubernetes client
        :param persist_config:
        """
        if config_file or not utils.is_running_in_k8s():
            config.load_kube_config(
                config_file=config_file,
                context=context,
                client_configuration=client_configuration,
                persist_config=persist_config,
            )
        else:
            config.load_incluster_config()

        self.custom_api = client.CustomObjectsApi()
        self.core_api = client.CoreV1Api()

    def create(self, paddlejob, namespace=utils.get_default_target_namespace()):
        """
        Create the PaddleJob
        :param paddlejob: PaddleJob object
        :param namespace: defaults to current or default namespace
        """

        try:
            self.custom_api.create_namespaced_custom_object(
                constants.KUBEFLOW_GROUP,
                constants.PADDLEJOB_VERSION,
                namespace,
                constants.PADDLEJOB_PLURAL,
                paddlejob,
            )
        except client.rest.ApiException as e:
            raise RuntimeError(
                "Exception when calling CustomObjectsApi->create_namespaced_custom_object:\
                 %s\n"
                % e
            )

        logging.info("PaddleJob {} has been created".format(paddlejob.metadata.name))

    def create_paddlejob_from_func(
        self,
        name: str,
        func: Callable,
        parameters: Dict[str, Any] = None,
        base_image: str = constants.PADDLEJOB_BASE_IMAGE,
        namespace: str = utils.get_default_target_namespace(),
        num_worker_replicas: int = None,
        packages_to_install: List[str] = None,
        pip_index_url: str = "https://pypi.org/simple",
    ):
        """Create PaddleJob from the function.

        Args:
            name: Name for the PaddleJob.
            func: Function that PaddleJob uses to train the model. This function
                must be Callable. Optionally, this function might have one dict
                argument to define input parameters for the function.
            parameters: Dict of input parameters that training function might receive.
            base_image: Image to use when executing the training function.
            namespace: Namespace for the PaddleJob.
            num_worker_replicas: Number of Worker replicas for the PaddleJob.
                If number of Worker replicas is 1, PaddleJob uses only
                Master replica.
            packages_to_install: List of Python packages to install in addition
                to the base image packages. These packages are installed before
                executing the objective function.
            pip_index_url: The PyPI url from which to install Python packages.
        """

        # Check if at least one worker replica is set.
        if num_worker_replicas is None:
            raise ValueError("At least one Worker replica for PaddleJob must be set")

        # Check if function is callable.
        if not callable(func):
            raise ValueError(
                f"Training function must be callable, got function type: {type(func)}"
            )

        # Get PaddleJob Pod template spec.
        pod_template_spec = utils.get_pod_template_spec(
            func=func,
            parameters=parameters,
            base_image=base_image,
            container_name="paddle",
            packages_to_install=packages_to_install,
            pip_index_url=pip_index_url,
        )

        # Create PaddleJob template.
        paddlejob = models.KubeflowOrgV1PaddleJob(
            api_version=f"{constants.KUBEFLOW_GROUP}/{constants.PADDLEJOB_VERSION}",
            kind=constants.PADDLEJOB_KIND,
            metadata=client.V1ObjectMeta(name=name, namespace=namespace),
            spec=models.KubeflowOrgV1PaddleJobSpec(
                run_policy=models.V1RunPolicy(clean_pod_policy=None),
                paddle_replica_specs={},
            ),
        )

        # Add Master and Worker replicas to the PaddleJob.
        paddlejob.spec.paddle_replica_specs["Master"] = models.V1ReplicaSpec(
            replicas=1, template=pod_template_spec,
        )

        # If number of Worker replicas is 1, PaddleJob uses only Master replica.
        if num_worker_replicas != 1:
            paddlejob.spec.paddle_replica_specs["Worker"] = models.V1ReplicaSpec(
                replicas=num_worker_replicas, template=pod_template_spec,
            )

        # Create PaddleJob
        self.create(paddlejob=paddlejob, namespace=namespace)

    def get(
        self, name=None, namespace=None, watch=False, timeout_seconds=600
    ):  # pylint: disable=inconsistent-return-statements
        """
        Get the paddlejob
        :param name: existing paddlejob name, if not defined, get all paddlejobs in the namespace.
        :param namespace: defaults to current or default namespace
        :param watch: Watch the paddlejob if `True`.
        :param timeout_seconds: How long to watch the paddlejob.
        :return: paddlejob
        """
        if namespace is None:
            namespace = utils.get_default_target_namespace()

        if name:
            if watch:
                paddlejob_watch(
                    name=name, namespace=namespace, timeout_seconds=timeout_seconds
                )
            else:
                thread = self.custom_api.get_namespaced_custom_object(
                    constants.KUBEFLOW_GROUP,
                    constants.PADDLEJOB_VERSION,
                    namespace,
                    constants.PADDLEJOB_PLURAL,
                    name,
                    async_req=True,
                )

                paddlejob = None
                try:
                    paddlejob = thread.get(constants.APISERVER_TIMEOUT)
                except multiprocessing.TimeoutError:
                    raise RuntimeError("Timeout trying to get PaddleJob.")
                except client.rest.ApiException as e:
                    raise RuntimeError(
                        "Exception when calling CustomObjectsApi->get_namespaced_custom_object:\
                        %s\n"
                        % e
                    )
                except Exception as e:
                    raise RuntimeError(
                        "There was a problem to get PaddleJob {0} in namespace {1}. Exception: \
                        {2} ".format(
                            name, namespace, e
                        )
                    )
                return paddlejob
        else:
            if watch:
                paddlejob_watch(namespace=namespace, timeout_seconds=timeout_seconds)
            else:
                thread = self.custom_api.list_namespaced_custom_object(
                    constants.KUBEFLOW_GROUP,
                    constants.PADDLEJOB_VERSION,
                    namespace,
                    constants.PADDLEJOB_PLURAL,
                    async_req=True,
                )

                paddlejob = None
                try:
                    paddlejob = thread.get(constants.APISERVER_TIMEOUT)
                except multiprocessing.TimeoutError:
                    raise RuntimeError("Timeout trying to get PaddleJob.")
                except client.rest.ApiException as e:
                    raise RuntimeError(
                        "Exception when calling CustomObjectsApi->list_namespaced_custom_object: \
                        %s\n"
                        % e
                    )
                except Exception as e:
                    raise RuntimeError(
                        "There was a problem to List PaddleJob in namespace {0}. \
                        Exception: {1} ".format(
                            namespace, e
                        )
                    )

                return paddlejob

    def patch(self, name, paddlejob, namespace=None):
        """
        Patch existing paddlejob
        :param name: existing paddlejob name
        :param paddlejob: patched paddlejob
        :param namespace: defaults to current or default namespace
        :return: patched paddlejob
        """
        if namespace is None:
            namespace = utils.set_paddlejob_namespace(paddlejob)

        try:
            outputs = self.custom_api.patch_namespaced_custom_object(
                constants.KUBEFLOW_GROUP,
                constants.PADDLEJOB_VERSION,
                namespace,
                constants.PADDLEJOB_PLURAL,
                name,
                paddlejob,
            )
        except client.rest.ApiException as e:
            raise RuntimeError(
                "Exception when calling CustomObjectsApi->patch_namespaced_custom_object:\
                 %s\n"
                % e
            )

        return outputs

    def delete(self, name, namespace=utils.get_default_target_namespace()):
        """
        Delete the PaddleJob
        :param name: PaddleJob name
        :param namespace: defaults to current or default namespace
        """

        try:
            self.custom_api.delete_namespaced_custom_object(
                group=constants.KUBEFLOW_GROUP,
                version=constants.PADDLEJOB_VERSION,
                namespace=namespace,
                plural=constants.PADDLEJOB_PLURAL,
                name=name,
                body=client.V1DeleteOptions(),
            )
        except client.rest.ApiException as e:
            raise RuntimeError(
                "Exception when calling CustomObjectsApi->delete_namespaced_custom_object:\
                 %s\n"
                % e
            )

        logging.info("PaddleJob {} has been deleted".format(name))

    def wait_for_job(
        self,
        name,  # pylint: disable=inconsistent-return-statements
        namespace=None,
        watch=False,
        timeout_seconds=600,
        polling_interval=30,
        status_callback=None,
    ):
        """Wait for the specified job to finish.

        :param name: Name of the PaddleJob.
        :param namespace: defaults to current or default namespace.
        :param timeout_seconds: How long to wait for the job.
        :param polling_interval: How often to poll for the status of the job.
        :param status_callback: (Optional): Callable. If supplied this callable is
               invoked after we poll the job. Callable takes a single argument which
               is the job.
        :return:
        """
        if namespace is None:
            namespace = utils.get_default_target_namespace()

        if watch:
            paddlejob_watch(
                name=name, namespace=namespace, timeout_seconds=timeout_seconds
            )
        else:
            return self.wait_for_condition(
                name,
                ["Succeeded", "Failed"],
                namespace=namespace,
                timeout_seconds=timeout_seconds,
                polling_interval=polling_interval,
                status_callback=status_callback,
            )

    def wait_for_condition(
        self,
        name,
        expected_condition,
        namespace=None,
        timeout_seconds=600,
        polling_interval=30,
        status_callback=None,
    ):
        """Waits until any of the specified conditions occur.

        :param name: Name of the job.
        :param expected_condition: A list of conditions. Function waits until any of the
               supplied conditions is reached.
        :param namespace: defaults to current or default namespace.
        :param timeout_seconds: How long to wait for the job.
        :param polling_interval: How often to poll for the status of the job.
        :param status_callback: (Optional): Callable. If supplied this callable is
               invoked after we poll the job. Callable takes a single argument which
               is the job.
        :return: Object: PaddleJob
        """

        if namespace is None:
            namespace = utils.get_default_target_namespace()

        for _ in range(round(timeout_seconds / polling_interval)):

            paddlejob = None
            paddlejob = self.get(name, namespace=namespace)

            if paddlejob:
                if status_callback:
                    status_callback(paddlejob)

                # If we poll the CRD quick enough status won't have been set yet.
                conditions = paddlejob.get("status", {}).get("conditions", [])
                # Conditions might have a value of None in status.
                conditions = conditions or []
                for c in conditions:
                    if c.get("type", "") in expected_condition:
                        return paddlejob

            time.sleep(polling_interval)

        raise RuntimeError(
            "Timeout waiting for PaddleJob {0} in namespace {1} to enter one of the "
            "conditions {2}.".format(name, namespace, expected_condition),
            paddlejob,
        )

    def get_job_status(self, name, namespace=None):
        """Returns PaddleJob status, such as Running, Failed or Succeeded.

        :param name: The PaddleJob name.
        :param namespace: defaults to current or default namespace.
        :return: str: PaddleJob status
        """
        if namespace is None:
            namespace = utils.get_default_target_namespace()

        paddlejob = self.get(name, namespace=namespace)
        last_condition = paddlejob.get("status", {}).get("conditions", [])[-1]
        return last_condition.get("type", "")

    def is_job_running(self, name, namespace=None):
        """Returns true if the PaddleJob running; false otherwise.

        :param name: The PaddleJob name.
        :param namespace: defaults to current or default namespace.
        :return: True or False
        """
        paddlejob_status = self.get_job_status(name, namespace=namespace)
        return paddlejob_status == constants.JOB_STATUS_RUNNING

    def is_job_succeeded(self, name, namespace=None):
        """Returns true if the PaddleJob succeeded; false otherwise.

        :param name: The PaddleJob name.
        :param namespace: defaults to current or default namespace.
        :return: True or False
        """
        paddlejob_status = self.get_job_status(name, namespace=namespace)
        return paddlejob_status == constants.JOB_STATUS_SUCCEEDED

    def get_pod_names(
        self,
        name,
        namespace=None,
        master=False,  # pylint: disable=inconsistent-return-statements
        replica_type=None,
        replica_index=None,
    ):
        """
        Get pod names of PaddleJob.
        :param name: PaddleJob name
        :param namespace: defaults to current or default namespace.
        :param master: Only get pod with label 'job-role: master' pod if True.
        :param replica_type: User can specify one of 'master, worker' to only get one type pods.
               By default get all type pods.
        :param replica_index: User can specfy replica index to get one pod of PaddleJob.
        :return: set: pods name
        """

        if namespace is None:
            namespace = utils.get_default_target_namespace()

        labels = utils.get_job_labels(
            name, master=master, replica_type=replica_type, replica_index=replica_index
        )

        try:
            resp = self.core_api.list_namespaced_pod(
                namespace, label_selector=utils.to_selector(labels)
            )
        except client.rest.ApiException as e:
            raise RuntimeError(
                "Exception when calling CoreV1Api->read_namespaced_pod_log: %s\n" % e
            )

        pod_names = []
        for pod in resp.items:
            if pod.metadata and pod.metadata.name:
                pod_names.append(pod.metadata.name)

        if not pod_names:
            logging.warning(
                "Not found Pods of the PaddleJob %s with the labels %s.", name, labels
            )
        else:
            return set(pod_names)

    def get_logs(
        self,
        name,
        namespace=None,
        master=False,
        replica_type=None,
        replica_index=None,
        follow=False,
        container="paddle",
    ):
        """
        Get training logs of the PaddleJob.
        By default only get the logs of Pod that has labels 'job-role: master'.
        :param container: container name
        :param name: PaddleJob name
        :param namespace: defaults to current or default namespace.
        :param master: By default get pod with label 'job-role: master' pod if True.
                       If need to get more Pod Logs, set False.
        :param replica_type: User can specify one of 'master, worker' to only get one type pods.
               By default get all type pods.
        :param replica_index: User can specfy replica index to get one pod of PaddleJob.
        :param follow: Follow the log stream of the pod. Defaults to false.
        :return: str: pods logs
        """

        if namespace is None:
            namespace = utils.get_default_target_namespace()

        pod_names = self.get_pod_names(
            name,
            namespace=namespace,
            master=master,
            replica_type=replica_type,
            replica_index=replica_index,
        )

        if pod_names:
            for pod in pod_names:
                try:
                    pod_logs = self.core_api.read_namespaced_pod_log(
                        pod, namespace, follow=follow, container=container
                    )
                    logging.info("The logs of Pod %s:\n %s", pod, pod_logs)
                except client.rest.ApiException as e:
                    raise RuntimeError(
                        "Exception when calling CoreV1Api->read_namespaced_pod_log: %s\n"
                        % e
                    )
        else:
            raise RuntimeError(
                "Not found Pods of the PaddleJob {} "
                "in namespace {}".format(name, namespace)
            )
