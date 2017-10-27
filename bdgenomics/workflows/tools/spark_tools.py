"""
Functions for calling raw tools in the UCSC Computational Genomics Lab
ADAM/Spark pipeline

@author Audrey Musselman-Brown, almussel@ucsc.edu
@author Frank Austin Nothaft, fnothaft@berkeley.
"""

import os.path
from subprocess import check_call, check_output

from toil.lib.docker import dockerCall
from toil_lib import require

SPARK_MASTER_PORT = "7077"
HDFS_MASTER_PORT = "8020"


class MasterAddress(str):
    """
    A string containing the hostname or IP of the Spark/HDFS master. The Spark master expects its own address to
    match what the client uses to connect to it. For example, if the master is configured with a host name,
    the driver can't use an IP address to connect to it, and vice versa. This class works around by distinguishing
    between the notional master address (self) and the actual one (self.actual) and adds support for the special
    master address "auto" in order to implement auto-discovery of the master of a standalone.

    >>> foo = MasterAddress('foo')
    >>> foo == 'foo'
    True
    >>> foo.actual == 'foo'
    True
    >>> foo.actual == foo
    True
    """
    def __init__(self, master_ip):
        super(MasterAddress, self).__init__(master_ip)
        self.actual = self

    def docker_parameters(self, docker_parameters=None):
        """
        Augment a list of "docker run" arguments with those needed to map the  notional Spark master address to the
        real one, if they are different.
        """
        if self != self.actual:
            add_host_option = '--add-host=spark-master:' + self.actual
            if docker_parameters is None:
                docker_parameters = [add_host_option]
            else:
                docker_parameters.append(add_host_option)
        return docker_parameters

def _make_parameters(master_ip, default_parameters, memory, arguments, override_parameters):
    """
    Makes a Spark Submit style job submission line.

    :param masterIP: The Spark leader IP address.
    :param default_parameters: Application specific Spark configuration parameters.
    :param memory: The memory to allocate to each Spark driver and executor.
    :param arguments: Arguments to pass to the submitted job.
    :param override_parameters: Parameters passed by the user, that override our defaults.

    :type masterIP: MasterAddress
    :type default_parameters: list of string
    :type arguments: list of string
    :type memory: int or None
    :type override_parameters: list of string or None
    """

    # python doesn't support logical xor?
    # anywho, exactly one of memory or override_parameters must be defined
    require((override_parameters is not None or memory is not None) and
            (override_parameters is None or memory is None),
            "Either the memory setting must be defined or you must provide Spark configuration parameters.")

    # if the user hasn't provided overrides, set our defaults
    parameters = []
    if memory is not None:
        parameters = ["--conf", "spark.driver.memory=%sg" % memory,
                      "--conf", "spark.executor.memory=%sg" % memory]
    else:
        parameters.extend(override_parameters)

    if master_ip:
        parameters.extend(["--master", "spark://%s:%s" % (master_ip, SPARK_MASTER_PORT),
                           "--conf", ("spark.hadoop.fs.default.name=hdfs://%s:%s" % (master_ip, HDFS_MASTER_PORT))])

    # add the tool specific spark parameters
    parameters.extend(default_parameters)

    # spark submit expects a '--' to split the spark conf arguments from tool arguments
    parameters.append('--')

    # now add the tool arguments and return
    parameters.extend(arguments)

    return parameters


def call_conductor(job, master_ip, src, dst, memory=None, override_parameters=None):
    """
    Invokes the Conductor container to copy files between S3 and HDFS and vice versa.
    Find Conductor at https://github.com/BD2KGenomics/conductor.

    :param toil.Job.job job: The Toil Job calling this function
    :param masterIP: The Spark leader IP address.
    :param src: URL of file to copy.
    :param src: URL of location to copy file to.
    :param memory: Gigabytes of memory to provision for Spark driver/worker.
    :param override_parameters: Parameters passed by the user, that override our defaults.

    :type masterIP: MasterAddress
    :type src: string
    :type dst: string
    :type memory: int or None
    :type override_parameters: list of string or None
    """

    arguments = ["-C", src, dst]

    docker_parameters = ['--log-driver', 'none', master_ip.docker_parameters(["--net=host"])]
    dockerCall(job=job,
                tool="quay.io/ucsc_cgl/conductor",
                parameters=_make_parameters(master_ip,
                                            [], # no conductor specific spark configuration
                                            memory,
                                            arguments,
                                            override_parameters),
               dockerParameters=docker_parameters)


def call_adam(job, master_ip, arguments,
              memory=None,
              override_parameters=None,
              run_local=False,
              native_adam_path=None):
    """
    Invokes the ADAM container. Find ADAM at https://github.com/bigdatagenomics/adam.

    :param toil.Job.job job: The Toil Job calling this function
    :param masterIP: The Spark leader IP address.
    :param arguments: Arguments to pass to ADAM.
    :param memory: Gigabytes of memory to provision for Spark driver/worker.
    :param override_parameters: Parameters passed by the user, that override our defaults.
    :param native_adam_path: Path to ADAM executable. If not provided, Docker is used.
    :param run_local: If true, runs Spark with the --master local[*] setting, which uses
      all cores on the local machine. The master_ip will be disregarded.

    :type masterIP: MasterAddress
    :type arguments: list of string
    :type memory: int or None
    :type override_parameters: list of string or None
    :type native_adam_path: string or None
    :type run_local: boolean
    """
    if run_local:
        master = ["--master", "local[*]"]
    else:
        master = ["--master",
                  ("spark://%s:%s" % (master_ip, SPARK_MASTER_PORT)),
                  "--conf", ("spark.hadoop.fs.default.name=hdfs://%s:%s" % (master_ip, HDFS_MASTER_PORT)),]

    default_params = (master + [
            # set max result size to unlimited, see #177
            "--conf", "spark.driver.maxResultSize=0",
            # these memory tuning parameters were derived in the course of running the
            # experiments for the ADAM sigmod paper:
            #
            # Nothaft, Frank Austin, et al. "Rethinking data-intensive science using scalable
            # analytics systems." Proceedings of the 2015 ACM SIGMOD International Conference
            # on Management of Data. ACM, 2015.
            #
            # the memory tunings reduce the amount of memory dedicated to caching, which we don't
            # take advantage of, and the network timeout flag reduces the number of job failures
            # caused by heavy gc load
            "--conf", "spark.storage.memoryFraction=0.3",
            "--conf", "spark.storage.unrollFraction=0.1",
            "--conf", "spark.network.timeout=300s"])

    # are we running adam via docker, or do we have a native path?
    if native_adam_path is None:
        docker_parameters = ['--log-driver', 'none', master_ip.docker_parameters(["--net=host"])]
        dockerCall(job=job,
                    tool="quay.io/ucsc_cgl/adam:962-ehf--6e7085f8cac4b9a927dc9fb06b48007957256b80",
                    dockerParameters=docker_parameters,
                    parameters=_make_parameters(master_ip,
                                                default_params,
                                                memory,
                                                arguments,
                                                override_parameters))
    else:
        check_call([os.path.join(native_adam_path, "bin/adam-submit")] +
                   default_params +
                   arguments)

def call_deca(job, master_ip, arguments,
              memory=None,
              override_parameters=None,
              work_dir=None,
              run_local=False,
              aws_access_key_id=None,
              aws_secret_access_key=None):
    """
    Invokes the DECA container. Find DECA at https://github.com/bigdatagenomics/deca.

    :param toil.Job.job job: The Toil Job calling this function
    :param masterIP: The Spark leader IP address.
    :param arguments: Arguments to pass to ADAM.
    :param memory: Gigabytes of memory to provision for Spark driver/worker.
    :param override_parameters: Parameters passed by the user, that override our defaults.
    :param run_local: If true, runs Spark with the --master local[*] setting, which uses
      all cores on the local machine. The master_ip will be disregarded.

    :type masterIP: MasterAddress
    :type arguments: list of string
    :type memory: int or None
    :type override_parameters: list of string or None
    :type native_adam_path: string or None
    :type run_local: boolean
    """
    if run_local:
        master = ["--master", "local[*]"]
    else:
        '''
        hostname = check_output(["hostname", "-i"])[:-1]
        master = ["--conf", "spark.driver.host=%s" % hostname]
        '''
        master = []
        pass

    default_params = (master + [
        # set max result size to unlimited, see #177
        "--conf", "spark.driver.maxResultSize=0",
        "--conf", "spark.hadoop.hadoopbam.bam.enable-bai-splitter=true",
        "--packages", "com.amazonaws:aws-java-sdk-pom:1.10.34,org.apache.hadoop:hadoop-aws:2.7.4",
        "--conf", "spark.hadoop.fs.s3a.impl=org.apache.hadoop.fs.s3a.S3AFileSystem"])

    docker_parameters = ['--log-driver', 'none',
                         '--net=host']

    if aws_access_key_id:

        require(aws_secret_access_key,
                'If AWS access key is passed, secret key must be defined')

        docker_parameters.extend(['-e', 'AWS_ACCESS_KEY_ID=%s' % aws_access_key_id,
                                  '-e', 'AWS_SECRET_ACCESS_KEY=%s' % aws_secret_access_key])

        default_params.extend(
            ["--packages", "com.amazonaws:aws-java-sdk-pom:1.10.34,org.apache.hadoop:hadoop-aws:2.7.4",
             "--conf", "spark.hadoop.fs.s3a.impl=org.apache.hadoop.fs.s3a.S3AFileSystem"])

        for scheme in ['s3', 's3n']:
            default_params.extend([
                "--conf", "spark.hadoop.fs.%s.awsAccessKeyId=%s" % (scheme, aws_access_key_id),
                "--conf", "spark.hadoop.fs.%s.awsSecretAccessKey=%s" % (scheme, aws_secret_access_key)])

        default_params.extend([
            "--conf", "spark.hadoop.fs.s3a.access.key=%s" % aws_access_key_id,
            "--conf", "spark.hadoop.fs.s3a.secret.key=%s" % aws_secret_access_key,
            "--conf", "spark.executorEnv.AWS_ACCESS_KEY_ID=%s" % aws_access_key_id,
            "--conf", "spark.executorEnv.AWS_SECRET_ACCESS_KEY=%s" % aws_secret_access_key])

    if work_dir:
        docker_parameters.extend(['-v', '%s:/data' % work_dir])

    dockerCall(job=job,
               tool="quay.io/ucsc_cgl/deca:0.1.0--7d13833a1220001481c4de0489e893c93ee3310f",
               dockerParameters=docker_parameters,
               parameters=_make_parameters(master_ip,
                                           default_params,
                                           memory,
                                           arguments,
                                           override_parameters))


def call_mango_browser(job, master_ip, arguments,
              host='127.0.0.1',
              port=8080,
              memory=None,
              override_parameters=None,
              work_dir=None,
              run_local=False,
              run_mac=False,
              aws_access_key_id=None,
              aws_secret_access_key=None):
    """
    Invokes the Mango browser container. Find mango at https://github.com/bigdatagenomics/mango.

    :param toil.Job.job job: The Toil Job calling this function
    :param masterIP: The Spark leader IP address.
    :param arguments: Arguments to pass to Mango.
    :param memory: Gigabytes of memory to provision for Spark driver/worker.
    :param override_parameters: Parameters passed by the user, that override our defaults.
    :param run_local: If true, runs Spark with the --master local[*] setting, which uses
      all cores on the local machine. The master_ip will be disregarded.

    :type masterIP: MasterAddress
    :type arguments: list of string
    :type memory: int or None
    :type override_parameters: list of string or None
    :type native_adam_path: string or None
    :type run_local: boolean
    """

    if run_local:
        master = ["--master", "local[*]"]
    else:
        '''
        hostname = check_output(["hostname", "-i"])[:-1]
        master = ["--conf", "spark.driver.host=%s" % hostname]
        '''
        master = []
        pass

    default_params = (master + [
        # set max result size to unlimited, see #177
        "--conf", "spark.driver.maxResultSize=0",
        "--conf", "spark.hadoop.hadoopbam.bam.enable-bai-splitter=true",
        "--packages", "com.amazonaws:aws-java-sdk-pom:1.10.34,org.apache.hadoop:hadoop-aws:2.7.4",
        "--conf", "spark.hadoop.fs.s3a.impl=org.apache.hadoop.fs.s3a.S3AFileSystem"])

    docker_parameters = []

    if not run_mac:
        docker_parameters.extend(['--net=host']) # for accessing localhost
    else:
        # port forwarding because we have not set --net=host
        endpoint = "{}:8080".format(port)
        docker_parameters.extend(['-p', endpoint])

    if aws_access_key_id:

        require(aws_secret_access_key,
                'If AWS access key is passed, secret key must be defined')

        docker_parameters.extend(['-e', 'AWS_ACCESS_KEY_ID=%s' % aws_access_key_id,
                                  '-e', 'AWS_SECRET_ACCESS_KEY=%s' % aws_secret_access_key])

        default_params.extend(
            [
             "--conf", "spark.hadoop.fs.s3a.impl=org.apache.hadoop.fs.s3a.S3AFileSystem"])

        for scheme in ['s3', 's3n']:
            default_params.extend([
                "--conf", "spark.hadoop.fs.%s.awsAccessKeyId=%s" % (scheme, aws_access_key_id),
                "--conf", "spark.hadoop.fs.%s.awsSecretAccessKey=%s" % (scheme, aws_secret_access_key)])

        default_params.extend([
            "--conf", "spark.hadoop.fs.s3a.access.key=%s" % aws_access_key_id,
            "--conf", "spark.hadoop.fs.s3a.secret.key=%s" % aws_secret_access_key,
            "--conf", "spark.executorEnv.AWS_ACCESS_KEY_ID=%s" % aws_access_key_id,
            "--conf", "spark.executorEnv.AWS_SECRET_ACCESS_KEY=%s" % aws_secret_access_key])

    if work_dir:
        docker_parameters.extend(['-v', '%s:/data' % work_dir])

    parameters=_make_parameters(master_ip,
                                default_params,
                                memory,
                                arguments,
                                override_parameters)

    job.fileStore.logToMaster("Starting the merge sort")
    job.fileStore.logToMaster(__name__)

    try:
        dockerCall(job=job,
                   tool="quay.io/ucsc_cgl/mango:latest",
                   dockerParameters=docker_parameters,
                   parameters=parameters)

    except:
        job.fileStore.logToMaster("docker exited")


def call_mango_notebook(job, master_ip, arguments,
              host='127.0.0.1',
              port=8888,
              memory=None,
              override_parameters=None,
              work_dir=None,
              run_local=False,
              run_mac=False,
              aws_access_key_id=None,
              aws_secret_access_key=None):
    """
    Invokes the Mango browser container. Find mango at https://github.com/bigdatagenomics/mango.

    :param toil.Job.job job: The Toil Job calling this function
    :param masterIP: The Spark leader IP address.
    :param arguments: Arguments to pass to ADAM.
    :param memory: Gigabytes of memory to provision for Spark driver/worker.
    :param override_parameters: Parameters passed by the user, that override our defaults.
    :param run_local: If true, runs Spark with the --master local[*] setting, which uses
      all cores on the local machine. The master_ip will be disregarded.

    :type masterIP: MasterAddress
    :type arguments: list of string
    :type memory: int or None
    :type override_parameters: list of string or None
    :type native_adam_path: string or None
    :type run_local: boolean
    """

    if run_local:
        master = ["--master", "local[*]"]
    else:
        '''
        hostname = check_output(["hostname", "-i"])[:-1]
        master = ["--conf", "spark.driver.host=%s" % hostname]
        '''
        master = []
        pass

    default_params = (master + [
        # set max result size to unlimited, see #177
        "--conf", "spark.driver.maxResultSize=0",
        "--conf", "spark.hadoop.hadoopbam.bam.enable-bai-splitter=true",
        "--packages", "com.amazonaws:aws-java-sdk-pom:1.10.34,org.apache.hadoop:hadoop-aws:2.7.4",
        "--conf", "spark.hadoop.fs.s3a.impl=org.apache.hadoop.fs.s3a.S3AFileSystem"])

    docker_parameters = []
    if not run_mac:
        docker_parameters.extend(['--net=host']) # for accessing localhost
    else:
        # port forwarding
        endpoint = "{}:8888".format(port)
        docker_parameters.extend(['-p', endpoint])

    # reconfigure entrypoint for notebook
    docker_parameters.extend(['--entrypoint=/home/mango/bin/mango-notebook'])

    if aws_access_key_id:

        require(aws_secret_access_key,
                'If AWS access key is passed, secret key must be defined')

        docker_parameters.extend(['-e', 'AWS_ACCESS_KEY_ID=%s' % aws_access_key_id,
                                  '-e', 'AWS_SECRET_ACCESS_KEY=%s' % aws_secret_access_key])

        default_params.extend(
            [
             "--conf", "spark.hadoop.fs.s3a.impl=org.apache.hadoop.fs.s3a.S3AFileSystem"])

        for scheme in ['s3', 's3n']:
            default_params.extend([
                "--conf", "spark.hadoop.fs.%s.awsAccessKeyId=%s" % (scheme, aws_access_key_id),
                "--conf", "spark.hadoop.fs.%s.awsSecretAccessKey=%s" % (scheme, aws_secret_access_key)])

        default_params.extend([
            "--conf", "spark.hadoop.fs.s3a.access.key=%s" % aws_access_key_id,
            "--conf", "spark.hadoop.fs.s3a.secret.key=%s" % aws_secret_access_key,
            "--conf", "spark.executorEnv.AWS_ACCESS_KEY_ID=%s" % aws_access_key_id,
            "--conf", "spark.executorEnv.AWS_SECRET_ACCESS_KEY=%s" % aws_secret_access_key])

    if work_dir:
        docker_parameters.extend(['-v', '%s:/data' % work_dir])

    parameters=_make_parameters(master_ip,
                                default_params,
                                memory,
                                arguments,
                                override_parameters)
    dockerCall(job=job,
               tool="quay.io/ucsc_cgl/mango:latest",
               dockerParameters=docker_parameters,
               parameters=parameters)
