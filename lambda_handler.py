import json
import boto3
import os
import base64
import time
import requests
from datetime import datetime, timedelta
from botocore.exceptions import ClientError
from typing import Dict, List, Optional

# Initialize AWS clients
sts = boto3.client('sts')
secrets_manager = boto3.client('secretsmanager')
sns = boto3.client('sns')
ec2 = boto3.client('ec2')
sqs = boto3.client('sqs')

# Environment variables - Replace these with your own values
AD_CREDENTIALS_SECRET = os.environ.get('AD_CREDENTIALS_SECRET', 'ad-credentials')
JIRA_CREDENTIALS_SECRET = os.environ.get('JIRA_CREDENTIALS_SECRET', 'jira-credentials')
M365_CREDENTIALS_SECRET = os.environ.get('M365_CREDENTIALS_SECRET', 'microsoft-365-credentials')
ERROR_TOPIC_ARN = os.environ.get('ERROR_TOPIC_ARN', 'arn:aws:sns:region:account:topic')
EMAIL_FORMAT = os.environ.get('EMAIL_FORMAT', 'firstname.lastname')
JIRA_URL = os.environ.get('JIRA_URL', 'https://your-company.atlassian.net')
OU_MAPPING_SECRET = os.environ.get('OU_MAPPING_SECRET', 'ou-mapping')

# New environment variables
M365_DELAY_QUEUE_URL = os.environ.get('M365_DELAY_QUEUE_URL', '')
ATLASSIAN_ENABLED = os.environ.get('ATLASSIAN', 'false').lower() == 'true'

# Cross-account configuration - Replace with your account details
PROD_ACCOUNT_ID = os.environ.get('PROD_ACCOUNT_ID', '123456789012')
CROSS_ACCOUNT_ROLE_NAME = os.environ.get('CROSS_ACCOUNT_ROLE_NAME', 'CrossAccountSSMRole')
EXTERNAL_ID = os.environ.get('EXTERNAL_ID', 'unique-external-id')

# Microsoft Graph API configuration
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_AUTH_URL = "https://login.microsoftonline.com"

# Global variables for cross-account clients
ssm_prod = None
ec2_prod = None


def get_secret(secret_name):
    """Retrieve secret from AWS Secrets Manager"""
    try:
        response = secrets_manager.get_secret_value(SecretId=secret_name)
        return json.loads(response['SecretString'])
    except ClientError as e:
        print(f"Error retrieving secret {secret_name}: {e}")
        return None


def get_cross_account_clients():
    """Get or create cross-account clients for production account"""
    global ssm_prod, ec2_prod
    
    if ssm_prod is None or ec2_prod is None:
        try:
            role_arn = f"arn:aws:iam::{PROD_ACCOUNT_ID}:role/{CROSS_ACCOUNT_ROLE_NAME}"
            
            print(f"Assuming role: {role_arn}")
            
            assumed_role = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName='EmployeeOnboardingLambda',
                ExternalId=EXTERNAL_ID
            )
            
            credentials = assumed_role['Credentials']
            
            ssm_prod = boto3.client(
                'ssm',
                aws_access_key_id=credentials['AccessKeyId'],
                aws_secret_access_key=credentials['SecretAccessKey'],
                aws_session_token=credentials['SessionToken']
            )
            
            ec2_prod = boto3.client(
                'ec2',
                aws_access_key_id=credentials['AccessKeyId'],
                aws_secret_access_key=credentials['SecretAccessKey'],
                aws_session_token=credentials['SessionToken']
            )
            
            print("Successfully created cross-account clients")
            
        except Exception as e:
            print(f"Error assuming cross-account role: {str(e)}")
            raise
    
    return ssm_prod, ec2_prod


def determine_event_type(event):
    """Determine if this is SNS (new user) or SQS (delayed M365) event"""
    
    if 'Records' in event:
        if len(event['Records']) > 0:
            record = event['Records'][0]
            
            # SNS event (new user onboarding)
            if 'Sns' in record:
                return 'sns_onboarding'
            
            # SQS event (delayed M365 processing)
            elif 'body' in record and 'eventSource' in record and record['eventSource'] == 'aws:sqs':
                return 'sqs_delayed_m365'
    
    return 'unknown'


class Microsoft365Manager:
    """Manage Microsoft 365 user creation and license assignment"""
    
    def __init__(self):
        self.access_token = None
        self.token_expires = None
        self.credentials = None
        
    def get_credentials(self):
        """Get Microsoft 365 credentials from AWS Secrets Manager"""
        if not self.credentials:
            self.credentials = get_secret(M365_CREDENTIALS_SECRET)
        return self.credentials
        
    def get_access_token(self):
        """Get Microsoft Graph API access token"""
        if self.access_token and self.token_expires and datetime.now() < self.token_expires:
            return self.access_token
            
        try:
            creds = self.get_credentials()
            
            # Prepare token request
            token_data = {
                'client_id': creds['client_id'],
                'client_secret': creds['client_secret'],
                'scope': 'https://graph.microsoft.com/.default',
                'grant_type': 'client_credentials'
            }
            
            headers = {'Content-Type': 'application/x-www-form-urlencoded'}
            
            response = requests.post(
                f"{GRAPH_AUTH_URL}/{creds['tenant_id']}/oauth2/v2.0/token",
                data=token_data,
                headers=headers,
                timeout=30
            )
            
            response.raise_for_status()
            token_info = response.json()
            self.access_token = token_info['access_token']
            expires_in = token_info.get('expires_in', 3600)
            self.token_expires = datetime.now() + timedelta(seconds=expires_in - 60)
            return self.access_token
                
        except Exception as e:
            print(f"Error getting Microsoft 365 access token: {str(e)}")
            raise
    
    def check_user_exists(self, user_email):
        """Check if user exists in Azure AD"""
        headers = {
            'Authorization': f'Bearer {self.get_access_token()}',
            'Content-Type': 'application/json'
        }
        
        try:
            response = requests.get(
                f"{GRAPH_BASE_URL}/users/{user_email}",
                headers=headers,
                timeout=15
            )
            
            return response.status_code == 200
            
        except Exception as e:
            print(f"Error checking user existence: {str(e)}")
            return False
    
    def assign_license_to_user(self, user_email, license_sku_id=None):
        """Assign license to user"""
        headers = {
            'Authorization': f'Bearer {self.get_access_token()}',
            'Content-Type': 'application/json'
        }
        
        try:
            # Find available license if not specified
            if not license_sku_id:
                # Get available licenses logic here
                pass
            
            # Prepare license assignment payload
            license_payload = {
                "addLicenses": [
                    {
                        "skuId": license_sku_id
                    }
                ],
                "removeLicenses": []
            }
            
            response = requests.post(
                f"{GRAPH_BASE_URL}/users/{user_email}/assignLicense",
                headers=headers,
                json=license_payload,
                timeout=30
            )
            
            if response.status_code in [200, 202]:
                print(f"Successfully assigned license to {user_email}")
                return {'success': True}
            else:
                raise Exception(f"Failed to assign license: {response.status_code}")
                
        except Exception as e:
            print(f"Error assigning license to {user_email}: {str(e)}")
            raise


class AtlassianManager:
    """Manage Atlassian account creation and access replication"""
    
    def __init__(self):
        self.jira_creds = None
        self.base_url = JIRA_URL
        
    def get_credentials(self):
        """Get Atlassian credentials"""
        if not self.jira_creds:
            self.jira_creds = get_secret(JIRA_CREDENTIALS_SECRET)
        return self.jira_creds
    
    def get_auth_headers(self):
        """Get authentication headers for Atlassian API"""
        creds = self.get_credentials()
        if not creds:
            return None
        
        auth_string = f"{creds['username']}:{creds['apiToken']}"
        auth_b64 = base64.b64encode(auth_string.encode('ascii')).decode('ascii')
        
        return {
            'Authorization': f'Basic {auth_b64}',
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
    
    def create_user(self, email: str, display_name: str) -> Dict:
        """Create a new Atlassian user"""
        headers = self.get_auth_headers()
        if not headers:
            return {'success': False, 'error': 'No credentials available'}
        
        try:
            create_payload = {
                "emailAddress": email,
                "displayName": display_name,
                "products": ["jira-software"]
            }
            
            response = requests.post(
                f"{self.base_url}/rest/api/3/user",
                headers=headers,
                json=create_payload,
                timeout=30
            )
            
            if response.status_code in [200, 201]:
                print(f"Successfully created Atlassian user: {email}")
                return {'success': True}
            else:
                return {'success': False, 'error': f"Failed with status {response.status_code}"}
                
        except Exception as e:
            print(f"Error creating Atlassian user: {str(e)}")
            return {'success': False, 'error': str(e)}


def execute_ps_script(script, instance_id):
    """Execute PowerShell script via SSM"""
    try:
        ssm_prod_client, _ = get_cross_account_clients()
        
        print(f"Executing PowerShell script on instance: {instance_id}")
        
        response = ssm_prod_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunPowerShellScript",
            Parameters={
                'commands': [script]
            },
            TimeoutSeconds=300
        )
        
        command_id = response['Command']['CommandId']
        print(f"SSM Command ID: {command_id}")
        
        # Wait for command to complete
        max_attempts = 30
        for i in range(max_attempts):
            time.sleep(2)
            
            try:
                result = ssm_prod_client.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=instance_id
                )
                
                if result['Status'] in ['Success', 'Failed']:
                    break
            except ClientError as e:
                if e.response['Error']['Code'] == 'InvocationDoesNotExist':
                    print(f"Waiting for command to be registered... (attempt {i+1}/{max_attempts})")
                    continue
                else:
                    raise
        
        if result['Status'] == 'Success':
            return result['StandardOutputContent']
        else:
            error_output = result.get('StandardErrorContent', 'No error details available')
            raise Exception(f"Command failed: {error_output}")
            
    except Exception as e:
        print(f"Error executing PowerShell script: {str(e)}")
        raise


def create_ad_user(employee_data, ou_path, domain, dc_instance_id):
    """Create Active Directory user"""
    
    # Generate username and email
    first_name = employee_data['firstName'].lower()
    last_name = employee_data['lastName'].lower()
    email = f"{first_name}.{last_name}@{domain}"
    username = f"{first_name}.{last_name}"[:20]  # Limit to 20 characters
    
    # PowerShell script for AD user creation
    ps_script = f"""
    Import-Module ActiveDirectory
    
    # Generate secure password
    $password = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 16 | ForEach-Object {{[char]$_}})
    $securePassword = ConvertTo-SecureString $password -AsPlainText -Force
    
    # Create user
    $userParams = @{{
        SamAccountName = '{username}'
        UserPrincipalName = '{email}'
        Name = '{employee_data['fullName']}'
        GivenName = '{employee_data['firstName']}'
        Surname = '{employee_data['lastName']}'
        DisplayName = '{employee_data['fullName']}'
        EmailAddress = '{email}'
        AccountPassword = $securePassword
        Enabled = $true
        ChangePasswordAtLogon = $true
        Path = '{ou_path}'
    }}
    
    try {{
        New-ADUser @userParams
        Write-Output "SUCCESS: Created user {username}"
        Write-Output "EMAIL: {email}"
        Write-Output "TEMPPASS: $password"
    }} catch {{
        Write-Output "ERROR: $_"
        exit 1
    }}
    """
    
    try:
        result = execute_ps_script(ps_script, dc_instance_id)
        
        return {
            'success': True,
            'username': username,
            'email': email,
            'domain': domain,
            'message': f"User {username} created successfully"
        }
    except Exception as e:
        print(f"Error creating AD user: {str(e)}")
        raise


def update_jira_ticket(ticket_key, message, success=True):
    """Update Jira ticket with status"""
    
    if not ticket_key or ticket_key.startswith('TEST-'):
        print(f"Skipping Jira update for: {ticket_key}")
        return
    
    try:
        jira_creds = get_secret(JIRA_CREDENTIALS_SECRET)
        
        auth_string = f"{jira_creds['username']}:{jira_creds['apiToken']}"
        auth_b64 = base64.b64encode(auth_string.encode('ascii')).decode('ascii')
        
        headers = {
            'Authorization': f'Basic {auth_b64}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        comment_body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": str(message)
                            }
                        ]
                    }
                ]
            }
        }
        
        response = requests.post(
            f"{JIRA_URL}/rest/api/3/issue/{ticket_key}/comment",
            json=comment_body,
            headers=headers,
            timeout=30
        )
        
        if response.status_code != 201:
            print(f"Failed to update Jira ticket: {response.status_code}")
            
    except Exception as e:
        print(f"Error updating Jira ticket: {str(e)}")


def lambda_handler(event, context):
    """Main Lambda handler for employee onboarding"""
    
    event_type = determine_event_type(event)
    
    try:
        if event_type == 'sns_onboarding':
            # Handle new user onboarding
            sns_record = event['Records'][0]['Sns']
            message_content = sns_record.get('Message', '')
            sns_message = json.loads(message_content)
            
            ticket_key = sns_message.get('ticketKey')
            employee_data = sns_message.get('employeeData', {})
            
            # Validate required fields
            required_fields = ['fullName', 'firstName', 'lastName']
            for field in required_fields:
                if not employee_data.get(field):
                    raise ValueError(f"Missing required field: {field}")
            
            # Update Jira - starting
            update_jira_ticket(
                ticket_key, 
                "🤖 Automated onboarding process started. Creating AD account..."
            )
            
            # Create AD user (simplified for example)
            ad_result = create_ad_user(
                employee_data,
                "OU=Users,DC=company,DC=com",  # Example OU
                "company.com",  # Example domain
                "i-1234567890abcdef"  # Example DC instance
            )
            
            # Update Jira with result
            update_jira_ticket(ticket_key, f"✅ AD account created: {ad_result['username']}")
            
            # Schedule M365 processing (if configured)
            if M365_DELAY_QUEUE_URL:
                message = {
                    'user_email': ad_result['email'],
                    'ticket_key': ticket_key,
                    'employee_data': employee_data
                }
                
                sqs.send_message(
                    QueueUrl=M365_DELAY_QUEUE_URL,
                    MessageBody=json.dumps(message),
                    DelaySeconds=900  # 15 minutes
                )
                
                update_jira_ticket(
                    ticket_key,
                    " Microsoft 365 setup scheduled for 15 minutes to allow AD sync"
                )
            
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'success': True,
                    'result': ad_result
                })
            }
            
        elif event_type == 'sqs_delayed_m365':
            # Handle delayed M365 processing
            sqs_message = json.loads(event['Records'][0]['body'])
            user_email = sqs_message['user_email']
            ticket_key = sqs_message.get('ticket_key')
            
            # Process M365 integration
            m365_manager = Microsoft365Manager()
            
            if m365_manager.check_user_exists(user_email):
                # Assign license
                m365_manager.assign_license_to_user(user_email)
                update_jira_ticket(
                    ticket_key,
                    f"✅ Microsoft 365 license assigned to {user_email}"
                )
            else:
                update_jira_ticket(
                    ticket_key,
                    f" User {user_email} not yet synced to Azure AD. Will retry."
                )
            
            # Process Atlassian if enabled
            if ATLASSIAN_ENABLED:
                atlassian_manager = AtlassianManager()
                display_name = sqs_message['employee_data'].get('fullName', '')
                result = atlassian_manager.create_user(user_email, display_name)
                
                if result.get('success'):
                    update_jira_ticket(
                        ticket_key,
                        f" Atlassian account created for {user_email}"
                    )
            
            return {
                'statusCode': 200,
                'body': json.dumps({'success': True})
            }
            
        else:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Unknown event type'})
            }
            
    except Exception as e:
        error_msg = str(e)
        print(f"Error in lambda_handler: {error_msg}")
        
        # Try to update Jira with error
        if 'ticket_key' in locals():
            update_jira_ticket(
                ticket_key,
                f" Onboarding failed: {error_msg}",
                success=False
            )
        
        return {
            'statusCode': 500,
            'body': json.dumps({
                'success': False,
                'error': error_msg
            })
        }
