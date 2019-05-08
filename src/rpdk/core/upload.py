import logging
from datetime import datetime

from botocore.exceptions import ClientError, WaiterError

from .data_loaders import resource_stream
from .exceptions import InternalError, UploadError

LOG = logging.getLogger(__name__)

BUCKET_OUTPUT_NAME = "CloudFormationManagedUploadBucketName"
INFRA_STACK_NAME = "CloudFormationManagedUploadInfrastructure"


class Uploader:
    def __init__(self, cfn_client, s3_client):
        self.cfn_client = cfn_client
        self.s3_client = s3_client

    @staticmethod
    def _get_template():
        with resource_stream(__name__, "data/managed-upload-infrastructure.yaml") as f:
            template = f.read()

        # sanity test! it's super easy to rename one but not the other
        if BUCKET_OUTPUT_NAME not in template:
            LOG.debug(
                "Output '%s' not found in managed upload infrastructure template:\n%s",
                BUCKET_OUTPUT_NAME,
                template,
            )
            raise InternalError(
                "Output not found in managed upload infrastructure template"
            )

        return template

    def _wait_for_stack(self, stack_id, waiter_name, success_msg):
        waiter = self.cfn_client.get_waiter(waiter_name)
        try:
            waiter.wait(
                StackName=stack_id, WaiterConfig={"Delay": 5, "MaxAttempts": 200}
            )
        except WaiterError as e:
            LOG.debug("Waiter failed for stack '%s'", stack_id, exc_info=e)
            LOG.critical(
                "Failed to create or update the managed upload infrastructure stack. "
                "This stack is in your account, so you may be able to self-help by "
                "looking at '%s'. Otherwise, please reach out to CloudFormation.",
                INFRA_STACK_NAME,
            )
            raise UploadError(
                "Failed to create or update the managed upload infrastructure stack"
            ) from e

        LOG.info(success_msg)

    def _get_stack_output(self, stack_id, output_key):
        result = self.cfn_client.describe_stacks(StackName=stack_id)
        outputs = result["Stacks"][0]["Outputs"]

        try:
            return next(
                output["OutputValue"]
                for output in outputs
                if output["OutputKey"] == output_key
            )
        except StopIteration:
            LOG.debug(
                "Outputs from stack '%s' did not contain '%s':\n%s",
                stack_id,
                output_key,
                ", ".join(output["OutputKey"] for output in outputs),
            )
            raise InternalError("Required output not found on stack")

    def _create_or_update_stack(self, template):
        args = {"StackName": INFRA_STACK_NAME, "TemplateBody": template}
        # attempt to create stack. if the stack already exists, try to update it
        LOG.info("Creating managed upload infrastructure stack")
        try:
            result = self.cfn_client.create_stack(
                **args, EnableTerminationProtection=True
            )
        except self.cfn_client.exceptions.AlreadyExistsException:
            LOG.info(
                "Managed upload infrastructure stack already exists. "
                "Attempting to update"
            )
            try:
                result = self.cfn_client.update_stack(**args)
            except ClientError as e:
                # if the update is a noop, don't do anything else
                msg = str(e)
                if "No updates are to be performed" in msg:
                    LOG.info("Managed upload infrastructure stack is up to date")
                    stack_id = INFRA_STACK_NAME
                else:
                    LOG.debug(
                        "Managed upload infrastructure stack update "
                        "resulted in unknown ClientError",
                        exc_info=e,
                    )
                    raise InternalError("Unknown CloudFormation error") from e
            else:
                stack_id = result["StackId"]
                self._wait_for_stack(
                    stack_id,
                    "stack_update_complete",
                    "Managed upload infrastructure stack is up to date",
                )
        except ClientError as e:
            LOG.debug(
                "Managed upload infrastructure stack create "
                "resulted in unknown ClientError",
                exc_info=e,
            )
            raise InternalError("Unknown CloudFormation error") from e
        else:
            stack_id = result["StackId"]
            self._wait_for_stack(
                stack_id,
                "stack_create_complete",
                "Managed upload infrastructure stack was successfully created",
            )

        return stack_id

    def upload(self, file_prefix, fileobj):
        template = self._get_template()
        stack_id = self._create_or_update_stack(template)
        bucket = self._get_stack_output(stack_id, BUCKET_OUTPUT_NAME)

        timestamp = datetime.utcnow().isoformat(timespec="seconds").replace(":", "-")
        key = "{}-{}.zip".format(file_prefix, timestamp)

        LOG.debug("Uploading to '%s/%s'...", bucket, key)
        try:
            self.s3_client.upload_fileobj(fileobj, bucket, key)
        except ClientError as e:
            LOG.debug("S3 upload resulted in unknown ClientError", exc_info=e)
            LOG.critical("Failed to upload artifacts to S3")
            raise UploadError("Failed to upload artifacts to S3: " + str(e)) from e

        LOG.debug("Upload complete")

        return "s3://{0}/{1}".format(bucket, key)