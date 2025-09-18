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

# Environment variables
AD_CREDENTIALS_SECRET = os.environ['AD_CREDENTIALS_SECRET']
JIRA_CREDENTIALS_SECRET = os.environ['JIRA_CREDENTIALS_SECRET']
M365_CREDENTIALS_SECRET = os.environ.get('M365_CREDENTIALS_SECRET', 'microsoft-365-credentials')
ERROR_TOPIC_ARN = os.environ['ERROR_TOPIC_ARN']
EMAIL_FORMAT = os.environ['EMAIL_FORMAT']
JIRA_URL = os.environ['JIRA_URL']
OU_MAPPING_SECRET = os.environ['OU_MAPPING_SECRET']

# New environment variables
M365_DELAY_QUEUE_URL = os.environ.get('M365_DELAY_QUEUE_URL', '')
ATLASSIAN_ENABLED = os.environ.get('ATLASSIAN', 'false').lower() == 'true'

# Cross-account configuration
PROD_ACCOUNT_ID = os.environ.get('PROD_ACCOUNT_ID', 'YOUR_ACCOUNT_ID')
CROSS_ACCOUNT_ROLE_NAME = os.environ.get('CROSS_ACCOUNT_ROLE_NAME', 'EmployeeOnboardingCrossAccountSSMRole')
EXTERNAL_ID = os.environ.get('EXTERNAL_ID', 'employee-onboarding-prod-access')

# Microsoft Graph API configuration
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_AUTH_URL = "https://login.microsoftonline.com"

# Global variables for cross-account clients
ssm_prod = None
ec2_prod = None

def schedule_m365_processing(user_email, ticket_key, employee_data, source_user_identifier=None, delay_seconds=900):
    """Schedule M365 processing with delay (default 15 minutes)"""
    
    if not M365_DELAY_QUEUE_URL:
        print("M365_DELAY_QUEUE_URL not configured, proceeding with immediate processing")
        return process_microsoft_365_integration_enhanced(user_email, source_user_identifier)
    
    try:
        # Create message for delayed processing
        delayed_message = {
            'processing_type': 'M365_INTEGRATION',
            'user_email': user_email,
            'ticket_key': ticket_key,
            'employee_data': employee_data,
            'source_user_identifier': source_user_identifier,
            'scheduled_time': datetime.now().isoformat(),
            'retry_count': 0
        }
        
        # Send message with delay
        response = sqs.send_message(
            QueueUrl=M365_DELAY_QUEUE_URL,
            MessageBody=json.dumps(delayed_message),
            DelaySeconds=delay_seconds  # 15 minutes = 900 seconds
        )
        
        print(f"Scheduled M365 processing for {user_email} in {delay_seconds/60} minutes")
        print(f"SQS Message ID: {response['MessageId']}")
        
        return {
            'scheduled': True,
            'delay_minutes': delay_seconds / 60,
            'message_id': response['MessageId'],
            'user_email': user_email
        }
        
    except Exception as e:
        print(f"Error scheduling M365 processing: {str(e)}")
        # Fallback to immediate processing
        return process_microsoft_365_integration_enhanced(user_email, source_user_identifier)

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
        """Check if user exists in Azure AD (quick check)"""
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
    
    def get_available_licenses(self):
        """Get available Microsoft 365 licenses"""
        headers = {
            'Authorization': f'Bearer {self.get_access_token()}',
            'Content-Type': 'application/json'
        }
        
        try:
            response = requests.get(
                f"{GRAPH_BASE_URL}/subscribedSkus",
                headers=headers,
                timeout=30
            )
            
            response.raise_for_status()
            skus = response.json()['value']
            available_licenses = []
            
            for sku in skus:
                if sku['prepaidUnits']['enabled'] > 0:
                    consumed = sku['consumedUnits']
                    available = sku['prepaidUnits']['enabled'] - consumed
                    
                    if available > 0:
                        available_licenses.append({
                            'skuId': sku['skuId'],
                            'skuPartNumber': sku['skuPartNumber'],
                            'available': available,
                            'total': sku['prepaidUnits']['enabled']
                        })
            
            return available_licenses
                
        except Exception as e:
            print(f"Error getting available licenses: {str(e)}")
            raise
    
    def find_business_premium_license(self):
        """Find Business Premium license SKU"""
        licenses = self.get_available_licenses()
        
        # Look for Business Premium licenses (common SKU part numbers)
        business_premium_patterns = [
            'SPB',  # Microsoft 365 Business Premium
            'O365_BUSINESS_PREMIUM',
            'BUSINESS_PREMIUM',
            'M365_BUSINESS_PREMIUM',
            'PREMIUM'
        ]
        
        # First, look for exact Business Premium matches
        for license in licenses:
            for pattern in business_premium_patterns[:3]:  # Check exact patterns first
                if pattern in license['skuPartNumber'].upper():
                    print(f"Found Business Premium license: {license['skuPartNumber']} ({license['available']} available)")
                    return license
        
        # Then look for any Premium license
        for license in licenses:
            if 'PREMIUM' in license['skuPartNumber'].upper() and license['available'] > 0:
                print(f"Found Premium license: {license['skuPartNumber']} ({license['available']} available)")
                return license
                
        # Return the first available license as fallback
        if licenses:
            print(f"No Premium license found, using: {licenses[0]['skuPartNumber']}")
            return licenses[0]
            
        raise Exception("No available Microsoft 365 licenses found")
    
    def set_user_usage_location(self, user_email, usage_location='GB'):
        """Set usage location for user to enable license assignment"""
        headers = {
            'Authorization': f'Bearer {self.get_access_token()}',
            'Content-Type': 'application/json'
        }
        
        try:
            payload = {
                "usageLocation": usage_location
            }
            
            response = requests.patch(
                f"{GRAPH_BASE_URL}/users/{user_email}",
                headers=headers,
                json=payload,
                timeout=30
            )
            
            if response.status_code in [200, 204]:
                print(f"Successfully set usage location {usage_location} for {user_email}")
                return True
            else:
                print(f"Failed to set usage location: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            print(f"Error setting usage location for {user_email}: {str(e)}")
            return False
    
    def assign_license_to_user(self, user_email, license_sku_id=None):
        """Assign Business Premium license to user"""
        headers = {
            'Authorization': f'Bearer {self.get_access_token()}',
            'Content-Type': 'application/json'
        }
        
        try:
            # If no specific license provided, find Business Premium
            if not license_sku_id:
                license_info = self.find_business_premium_license()
                license_sku_id = license_info['skuId']
                license_name = license_info['skuPartNumber']
            else:
                license_name = license_sku_id
            
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
                print(f"Successfully assigned license {license_name} to {user_email}")
                return {
                    'success': True,
                    'license_sku_id': license_sku_id,
                    'license_name': license_name,
                    'message': f'License {license_name} assigned to {user_email}'
                }
            else:
                error_detail = response.text
                raise Exception(f"Failed to assign license: {response.status_code} - {error_detail}")
                
        except Exception as e:
            print(f"Error assigning license to {user_email}: {str(e)}")
            raise
    
    def get_user_groups(self, user_email):
        """Get all groups a user belongs to"""
        headers = {
            'Authorization': f'Bearer {self.get_access_token()}',
            'Content-Type': 'application/json'
        }
        
        try:
            response = requests.get(
                f"{GRAPH_BASE_URL}/users/{user_email}/memberOf",
                headers=headers,
                timeout=30
            )
            
            if response.status_code == 200:
                groups = response.json()['value']
                return [{'id': group['id'], 'displayName': group['displayName']} for group in groups]
            else:
                raise Exception(f"Failed to get user groups: {response.status_code} - {response.text}")
                
        except Exception as e:
            print(f"Error getting groups for {user_email}: {str(e)}")
            return []
    
    def add_user_to_group(self, user_email, group_id):
        """Add user to a specific group with better error handling"""
        headers = {
            'Authorization': f'Bearer {self.get_access_token()}',
            'Content-Type': 'application/json'
        }
        
        try:
            # First, check if this is a mail-enabled security group or other problematic type
            group_response = requests.get(
                f"{GRAPH_BASE_URL}/groups/{group_id}",
                headers=headers,
                timeout=30
            )
            
            if group_response.status_code == 200:
                group_data = group_response.json()
                group_name = group_data.get('displayName', 'Unknown')
                
                # Skip mail-enabled security groups as they can't be modified via Graph API
                if group_data.get('mailEnabled') and group_data.get('securityEnabled'):
                    print(f"Skipping mail-enabled security group: {group_name} (requires Exchange management)")
                    return False
                
                # Skip dynamic groups
                if group_data.get('membershipRule'):
                    print(f"Skipping dynamic group: {group_name}")
                    return False
                
                # Skip certain system groups
                if group_name.lower() in ['all users', 'all company', 'everyone']:
                    print(f"Skipping system group: {group_name}")
                    return False
            
            # Get user object ID
            user_response = requests.get(
                f"{GRAPH_BASE_URL}/users/{user_email}",
                headers=headers,
                timeout=30
            )
            
            if user_response.status_code != 200:
                raise Exception(f"Failed to get user details: {user_response.status_code}")
            
            user_id = user_response.json()['id']
            
            # Add user to group
            payload = {
                "@odata.id": f"{GRAPH_BASE_URL}/directoryObjects/{user_id}"
            }
            
            response = requests.post(
                f"{GRAPH_BASE_URL}/groups/{group_id}/members/$ref",
                headers=headers,
                json=payload,
                timeout=30
            )
            
            if response.status_code in [204, 200]:
                return True
            elif response.status_code == 400 and "already exist" in response.text.lower():
                return True
            elif response.status_code == 403:
                print(f"Insufficient permissions for group {group_id}")
                return False
            elif response.status_code == 400 and "mail-enabled" in response.text.lower():
                print(f"Cannot modify mail-enabled group {group_id} via Graph API")
                return False
            else:
                print(f"Failed to add user to group {group_id}: {response.status_code}")
                return False
                
        except Exception as e:
            print(f"Error adding user {user_email} to group {group_id}: {str(e)}")
            return False
    
    def find_user_by_email_or_name(self, search_term):
        """Find user in Azure AD by email or display name"""
        headers = {
            'Authorization': f'Bearer {self.get_access_token()}',
            'Content-Type': 'application/json'
        }
        
        try:
            # Try direct email lookup first
            if '@' in search_term:
                response = requests.get(
                    f"{GRAPH_BASE_URL}/users/{search_term}",
                    headers=headers,
                    timeout=30
                )
                
                if response.status_code == 200:
                    return response.json()
            
            # Try search by display name or email
            search_response = requests.get(
                f"{GRAPH_BASE_URL}/users?$filter=displayName eq '{search_term}' or mail eq '{search_term}' or userPrincipalName eq '{search_term}'",
                headers=headers,
                timeout=30
            )
            
            if search_response.status_code == 200:
                users = search_response.json()['value']
                if users:
                    return users[0]
            
            return None
            
        except Exception as e:
            print(f"Error finding user {search_term}: {str(e)}")
            return None
    
    def replicate_m365_access(self, source_user_identifier, target_user_email):
        """Replicate Microsoft 365 access from source user to target user"""
        try:
            print(f"Replicating M365 access from {source_user_identifier} to {target_user_email}")
            
            # Find source user
            source_user = self.find_user_by_email_or_name(source_user_identifier)
            if not source_user:
                raise Exception(f"Source user {source_user_identifier} not found in Azure AD")
            
            source_email = source_user.get('mail') or source_user.get('userPrincipalName')
            print(f"Found source user: {source_user['displayName']} ({source_email})")
            
            # Get source user's groups
            source_groups = self.get_user_groups(source_email)
            
            replication_results = {
                'source_user': source_user['displayName'],
                'source_email': source_email,
                'groups_added': [],
                'groups_failed': [],
                'groups_skipped': [],
                'total_groups': len(source_groups)
            }
            
            # Add target user to same groups
            for group in source_groups:
                success = self.add_user_to_group(target_user_email, group['id'])
                if success:
                    replication_results['groups_added'].append(group['displayName'])
                    print(f"Added {target_user_email} to group: {group['displayName']}")
                else:
                    # Check if it was skipped or failed
                    if 'mail-enabled' in group.get('displayName', '').lower() or 'skipping' in str(success):
                        replication_results['groups_skipped'].append(group['displayName'])
                    else:
                        replication_results['groups_failed'].append(group['displayName'])
                    print(f"Could not add {target_user_email} to group: {group['displayName']}")
            
            return replication_results
            
        except Exception as e:
            print(f"Error replicating M365 access: {str(e)}")
            raise

class AtlassianManager:
    """Manage Atlassian account creation and access replication using Admin API"""
    
    def __init__(self):
        self.jira_creds = None
        self.base_url = None
        self.org_id = None
        
    def get_credentials(self):
        """Get Atlassian credentials from existing Jira credentials"""
        if not self.jira_creds:
            try:
                # Use existing Jira credentials for Atlassian
                self.jira_creds = get_secret(JIRA_CREDENTIALS_SECRET)
                self.base_url = JIRA_URL  # https://your-company.atlassian.net
                
                # Get organization ID if needed
                self.get_organization_id()
                
                return self.jira_creds
            except Exception as e:
                print(f"Error getting Atlassian credentials: {str(e)}")
                return None
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
    
    def get_organization_id(self):
        """Get the Atlassian organization ID"""
        headers = self.get_auth_headers()
        if not headers:
            return None
        
        try:
            # Get accessible resources to find org ID
            response = requests.get(
                'https://api.atlassian.com/oauth/token/accessible-resources',
                headers=headers,
                timeout=30
            )
            
            if response.status_code == 200:
                resources = response.json()
                if resources and len(resources) > 0:
                    self.org_id = resources[0].get('id')
                    print(f"Found organization ID: {self.org_id}")
                    return self.org_id
        except Exception as e:
            print(f"Could not get organization ID: {str(e)}")
        
        return None
    
    def check_user_exists(self, email: str) -> Dict:
        """Check if user exists in Atlassian and get their details"""
        headers = self.get_auth_headers()
        if not headers:
            return {'exists': False}
        
        try:
            # Search for user in Jira
            response = requests.get(
                f"{self.base_url}/rest/api/3/user/search?query={email}",
                headers=headers,
                timeout=30
            )
            
            if response.status_code == 200:
                users = response.json()
                if users and len(users) > 0:
                    user = users[0]
                    return {
                        'exists': True,
                        'accountId': user.get('accountId'),
                        'displayName': user.get('displayName'),
                        'emailAddress': user.get('emailAddress'),
                        'active': user.get('active', False)
                    }
            
            return {'exists': False}
            
        except Exception as e:
            print(f"Error checking user existence: {str(e)}")
            return {'exists': False, 'error': str(e)}
    
    def get_available_products(self):
        """Get available products in the Atlassian instance"""
        headers = self.get_auth_headers()
        if not headers:
            return []
        
        try:
            # Try to get accessible resources first
            response = requests.get(
                'https://api.atlassian.com/oauth/token/accessible-resources',
                headers=headers,
                timeout=30
            )
            
            if response.status_code == 200:
                resources = response.json()
                products = []
                for resource in resources:
                    if 'name' in resource:
                        # Extract product identifiers from the resource
                        products.append(resource.get('name', '').lower())
                print(f"Found accessible resources: {products}")
                return products
        except Exception as e:
            print(f"Could not get accessible resources: {str(e)}")
        
        # Return default products as fallback
        return []
    
    def create_user(self, email: str, display_name: str, send_invite: bool = True) -> Dict:
        """Create a new Atlassian user using the API with product access"""
        headers = self.get_auth_headers()
        if not headers:
            return {'success': False, 'error': 'No credentials available'}
        
        try:
            # Check if user already exists
            existing_user = self.check_user_exists(email)
            if existing_user.get('exists'):
                print(f"User {email} already exists in Atlassian")
                
                # Even if user exists, ensure they have all products including JSM
                account_id = existing_user.get('accountId')
                if account_id:
                    self._ensure_all_product_access(account_id, email)
                
                return {
                    'success': True,
                    'user_existed': True,
                    'accountId': account_id,
                    'message': 'User already exists'
                }
            
            print(f"Creating user via Jira API: {email}")
            
            # Since the API requires products, try different combinations
            # Including JSM in various formats
            product_combinations = [
                ["jira-software"],  # This worked in your environment
                ["jira-software", "jira-servicemanagement"],  # Add JSM
                ["jira-software", "jira-service-management"],  # Alternative JSM
                ["jira-software", "jira-service-desk"],  # Legacy JSM name
                ["jira-software", "servicedesk"],  # Short JSM name
                ["jira-software", "confluence"],  # Try with Confluence
                ["jira-software", "confluence", "jira-servicemanagement"],  # All products
                ["jira"],  # Simple name
                ["jira-core"],  # Jira Core
            ]
            
            # Try each product combination
            response = None
            successful_products = None
            
            for attempt, products in enumerate(product_combinations, 1):
                print(f"Attempt {attempt}: Trying with products: {products}")
                
                create_payload = {
                    "emailAddress": email,
                    "displayName": display_name,
                    "products": products
                }
                
                response = requests.post(
                    f"{self.base_url}/rest/api/3/user",
                    headers=headers,
                    json=create_payload,
                    timeout=30
                )
                
                if response.status_code in [200, 201]:
                    print(f"Success with products: {products}")
                    successful_products = products
                    break
                elif response.status_code == 400:
                    error_text = response.text
                    if "Invalid Jira product name" in error_text:
                        print(f"Invalid product names: {products}")
                        continue
                    elif "must specify the products" in error_text:
                        print(f"Products required but {products} not accepted")
                        continue
                else:
                    print(f"Attempt {attempt} failed: {response.status_code}")
            
            # Check if any attempt succeeded
            if response and response.status_code in [200, 201]:
                user_data = response.json()
                print(f"Successfully created Atlassian user: {email}")
                
                # After creating user, ensure they have ALL product access including JSM
                account_id = user_data.get('accountId') or user_data.get('key')
                if account_id:
                    print(f"User created with accountId: {account_id}")
                    
                    # Check if JSM was included in successful products
                    jsm_included = successful_products and any(
                        'service' in p.lower() or 'jsd' in p.lower() 
                        for p in successful_products
                    )
                    
                    if not jsm_included:
                        print("JSM not included in initial creation, adding it now...")
                    
                    # Always ensure all products including JSM
                    self._ensure_all_product_access(account_id, email)
                else:
                    print("Warning: No accountId returned, user may need manual product assignment")
                
                return {
                    'success': True,
                    'user_created': True,
                    'accountId': account_id,
                    'displayName': user_data.get('displayName'),
                    'message': f'User {email} created successfully with products: {successful_products}',
                    'products_assigned': successful_products,
                    'invite_sent': send_invite
                }
            else:
                # All attempts failed - provide detailed error
                error_msg = f"Failed to create user after all attempts. Last error: {response.status_code if response else 'No response'} - {response.text if response else 'No response'}"
                print(error_msg)
                
                return {
                    'success': False, 
                    'error': error_msg,
                    'diagnostic': 'API requires products but could not find valid product combination.'
                }
                
        except Exception as e:
            error_msg = f"Error creating Atlassian user: {str(e)}"
            print(error_msg)
            return {'success': False, 'error': error_msg}
    
    def _ensure_all_product_access(self, account_id: str, email: str):
        """Ensure user has access to all products including JSM Customer access"""
        headers = self.get_auth_headers()
        if not headers:
            return
        
        try:
            # Extract organization name from the base URL (e.g., Company-name from company-name.atlassian.net)
            org_name = self.base_url.split('//')[1].split('.')[0] if self.base_url else 'Your_Company_Name'
            
            # Groups that provide product access
            # Including JSM customer access groups with organization-specific naming
            product_access_groups = [
                "jira-software-users",
                "confluence-users",
                "jira-users",
                "users",
                # JSM Customer access groups - with organization name
                f"jira-servicemanagement-customers-{org_name}",  # This is the correct format!
                f"jira-service-management-customers-{org_name}",
                f"jira-servicedesk-customers-{org_name}",
                # Also try without org name in case
                "jira-servicemanagement-customers",
                "jira-service-management-customers", 
                "jira-servicedesk-customers",
                "service-desk-customers",
                "servicedesk-customers",
                "jsd-customers",
            ]
            
            groups_added = []
            jsm_added = False
            
            for group_name in product_access_groups:
                try:
                    response = requests.post(
                        f"{self.base_url}/rest/api/3/group/user",
                        headers=headers,
                        params={'groupname': group_name},
                        json={'accountId': account_id},
                        timeout=30
                    )
                    
                    if response.status_code in [200, 201]:
                        print(f"Added {email} to product group: {group_name}")
                        groups_added.append(group_name)
                        if 'servicemanagement-customers' in group_name:
                            jsm_added = True
                            print(f" JSM Customer access granted via group: {group_name}")
                    elif response.status_code == 400 and 'already a member' in response.text.lower():
                        print(f"User already in group: {group_name}")
                        groups_added.append(group_name)
                        if 'servicemanagement-customers' in group_name:
                            jsm_added = True
                    elif response.status_code == 404:
                        # Group doesn't exist, try next
                        continue
                    else:
                        print(f"Could not add to {group_name}: {response.status_code}")
                        
                except Exception as e:
                    print(f"Error adding to {group_name}: {str(e)}")
            
            # If JSM customer group was not found, try other methods
            if not jsm_added:
                print(f"JSM customer group not added yet, attempting alternative methods...")
                self._add_jsm_product_access(account_id, email)
            else:
                print(f"JSM Customer access successfully granted for {email}")
            
            # Also call the original method for backward compatibility
            self._ensure_product_access(account_id, email)
                    
        except Exception as e:
            print(f"Error ensuring all product access: {str(e)}")
    
    def _add_jsm_product_access(self, account_id: str, email: str):
        """Specifically add JSM product access to user"""
        headers = self.get_auth_headers()
        if not headers:
            return
        
        try:
            # Try to add JSM product access through the user management API
            # This varies by Atlassian configuration but let's try common approaches
            
            # Approach 1: Try to update user's product access
            products_to_try = [
                "jira-servicemanagement",
                "jira-service-management",
                "jira-service-desk",
                "servicedesk",
                "service-management"
            ]
            
            for product_key in products_to_try:
                try:
                    # Try adding product via user update
                    update_payload = {
                        "products": [product_key]
                    }
                    
                    response = requests.patch(
                        f"{self.base_url}/rest/api/3/user?accountId={account_id}",
                        headers=headers,
                        json=update_payload,
                        timeout=30
                    )
                    
                    if response.status_code in [200, 204]:
                        print(f"Successfully added JSM product access with key: {product_key}")
                        return True
                    else:
                        print(f"Could not add JSM with key {product_key}: {response.status_code}")
                        
                except Exception as e:
                    print(f"Error adding JSM product {product_key}: {str(e)}")
            
            # Approach 2: Create a service desk customer (this often auto-grants JSM access)
            try:
                # Get the first service desk project
                sd_response = requests.get(
                    f"{self.base_url}/rest/servicedeskapi/servicedesk",
                    headers=headers,
                    timeout=30
                )
                
                if sd_response.status_code == 200:
                    service_desks = sd_response.json().get('values', [])
                    if service_desks:
                        first_sd = service_desks[0]
                        sd_id = first_sd.get('id')
                        
                        # Add user as customer to this service desk
                        customer_payload = {
                            "accountIds": [account_id]
                        }
                        
                        add_customer_response = requests.post(
                            f"{self.base_url}/rest/servicedeskapi/servicedesk/{sd_id}/customer",
                            headers=headers,
                            json=customer_payload,
                            timeout=30
                        )
                        
                        if add_customer_response.status_code in [200, 204]:
                            print(f"Added user as customer to service desk {sd_id}")
                            return True
                        else:
                            print(f"Could not add as service desk customer: {add_customer_response.status_code}")
                            
            except Exception as e:
                print(f"Error adding as service desk customer: {str(e)}")
            
            print("WARNING: Could not add JSM product access through any method")
            return False
            
        except Exception as e:
            print(f"Error in JSM product access: {str(e)}")
            return False
    
    def _ensure_product_access(self, account_id: str, email: str):
        """Ensure user has access to Jira and Confluence products"""
        headers = self.get_auth_headers()
        if not headers:
            return
        
        try:
            # Add user to default product access groups
            # Try multiple group name variations as they differ between instances
            default_groups_variations = [
                ["jira-software-users", "confluence-users"],  # Standard names
                ["jira-users", "confluence-users"],  # Alternative names
                ["users", "confluence"],  # Simple names
                ["jira", "wiki-users"],  # Legacy names
            ]
            
            groups_added = []
            
            for group_set in default_groups_variations:
                for group_name in group_set:
                    if any(g in group_name for g in groups_added):
                        continue  # Skip if we already added a similar group
                    
                    try:
                        # Try to add user to product access group
                        response = requests.post(
                            f"{self.base_url}/rest/api/3/group/user",
                            headers=headers,
                            params={'groupname': group_name},
                            json={'accountId': account_id},
                            timeout=30
                        )
                        
                        if response.status_code in [200, 201]:
                            print(f"Added {email} to product group: {group_name}")
                            groups_added.append(group_name)
                        elif response.status_code == 400 and 'already a member' in response.text.lower():
                            print(f"User already in group: {group_name}")
                            groups_added.append(group_name)
                        elif response.status_code == 404:
                            print(f"Group {group_name} not found, trying next...")
                        else:
                            print(f"Could not add to {group_name}: {response.status_code}")
                                
                    except Exception as e:
                        print(f"Error adding to {group_name}: {str(e)}")
                
                # If we successfully added to at least one Jira and one Confluence group, we're done
                if any('jira' in g.lower() for g in groups_added) and any('confluence' in g.lower() for g in groups_added):
                    print(f"Successfully ensured product access for {email}")
                    break
                    
        except Exception as e:
            print(f"Error ensuring product access: {str(e)}")
    
    def get_user_groups(self, user_email: str) -> List[Dict]:
        """Get all groups a user belongs to in Atlassian/Jira"""
        headers = self.get_auth_headers()
        if not headers:
            return []
        
        try:
            # First get user details
            user_info = self.check_user_exists(user_email)
            if not user_info.get('exists'):
                print(f"User {user_email} not found")
                return []
            
            account_id = user_info['accountId']
            print(f"Getting groups for user {user_email} (accountId: {account_id})")
            
            # Method 1: Try the groups endpoint directly (most reliable)
            try:
                groups_response = requests.get(
                    f"{self.base_url}/rest/api/3/user/groups?accountId={account_id}",
                    headers=headers,
                    timeout=30
                )
                
                if groups_response.status_code == 200:
                    groups = groups_response.json()
                    if groups and len(groups) > 0:
                        group_list = [{'name': g.get('name'), 'groupId': g.get('groupId')} for g in groups]
                        print(f"Found {len(group_list)} groups via direct endpoint")
                        return group_list
            except Exception as e:
                print(f"Direct groups endpoint failed: {str(e)}")
            
            # Method 2: Try the bulk endpoint with expand parameter
            try:
                response = requests.get(
                    f"{self.base_url}/rest/api/3/user/bulk?accountId={account_id}&expand=groups",
                    headers=headers,
                    timeout=30
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if 'values' in data and len(data['values']) > 0:
                        user_data = data['values'][0]
                        groups = user_data.get('groups', {}).get('items', [])
                        
                        if groups:
                            group_list = []
                            for group in groups:
                                group_list.append({
                                    'name': group.get('name'),
                                    'groupId': group.get('groupId')
                                })
                            print(f"Found {len(group_list)} groups via bulk endpoint")
                            return group_list
            except Exception as e:
                print(f"Bulk endpoint failed: {str(e)}")
            
            # Method 3: Try searching for groups and checking membership
            try:
                # Get all groups in the instance
                all_groups_response = requests.get(
                    f"{self.base_url}/rest/api/3/group/bulk",
                    headers=headers,
                    timeout=30
                )
                
                if all_groups_response.status_code == 200:
                    all_groups = all_groups_response.json().get('values', [])
                    user_groups = []
                    
                    # Check each group for user membership
                    for group in all_groups[:50]:  # Limit to first 50 to avoid timeout
                        group_name = group.get('name')
                        if not group_name:
                            continue
                        
                        # Check if user is in this group
                        member_check = requests.get(
                            f"{self.base_url}/rest/api/3/group/member?groupname={group_name}&accountId={account_id}",
                            headers=headers,
                            timeout=5
                        )
                        
                        if member_check.status_code == 200:
                            is_member = member_check.json()
                            if is_member:
                                user_groups.append({
                                    'name': group_name,
                                    'groupId': group.get('groupId')
                                })
                    
                    if user_groups:
                        print(f"Found {len(user_groups)} groups via membership check")
                        return user_groups
            except Exception as e:
                print(f"Group membership check failed: {str(e)}")
            
            # If all methods fail, return empty list
            print(f"WARNING: Could not retrieve groups for {user_email}. User may not be in any groups or API access may be limited.")
            return []
            
        except Exception as e:
            print(f"Error getting user groups: {str(e)}")
            return []
    
    def add_user_to_group(self, user_email: str, group_name: str) -> bool:
        """Add user to a Jira/Confluence group"""
        headers = self.get_auth_headers()
        if not headers:
            print(f"No auth headers available for adding to group {group_name}")
            return False
        
        try:
            # Get user account ID
            user_info = self.check_user_exists(user_email)
            if not user_info.get('exists'):
                print(f"User {user_email} not found when trying to add to group {group_name}")
                return False
            
            account_id = user_info['accountId']
            print(f"Attempting to add user {user_email} (ID: {account_id}) to group: {group_name}")
            
            # IMPORTANT: Skip the membership check as it's giving false positives
            # The GET /group/member endpoint seems to be unreliable
            # Instead, we'll just try to add the user and handle the response
            
            print(f"Sending POST request to add user to {group_name} (skipping membership check)")
            
            # Add user to group using POST request
            response = requests.post(
                f"{self.base_url}/rest/api/3/group/user",
                headers=headers,
                params={'groupname': group_name},
                json={'accountId': account_id},
                timeout=30
            )
            
            print(f"Add to group response for {group_name}: Status={response.status_code}")
            
            if response.status_code in [200, 201]:
                print(f" Successfully added {user_email} to group: {group_name}")
                return True
            elif response.status_code == 204:
                # 204 No Content often means success
                print(f" Added {user_email} to group (204 response): {group_name}")
                return True
            elif response.status_code == 400:
                # Parse the error message
                error_text = response.text
                print(f"400 error for group {group_name}: {error_text[:500]}")
                
                error_lower = error_text.lower()
                if 'already a member' in error_lower or 'already in' in error_lower or 'user is already a member' in error_lower:
                    print(f"User already in group (per API response): {group_name}")
                    return True
                elif 'cannot add users to' in error_lower or 'cannot be modified' in error_lower:
                    print(f"Cannot add users to group {group_name} - may be a system/restricted group")
                    return False
                elif 'does not exist' in error_lower or 'group not found' in error_lower:
                    print(f"Group {group_name} does not exist")
                    return False
                elif 'not found' in error_lower:
                    print(f"Group or user not found for: {group_name}")
                    return False
                elif 'permission' in error_lower or 'not authorized' in error_lower:
                    print(f"No permission to add users to group: {group_name}")
                    return False
                else:
                    print(f"Unknown 400 error for group {group_name}. Full error: {error_text}")
                    # Try to parse JSON error if possible
                    try:
                        import json
                        error_json = json.loads(error_text)
                        if 'errorMessages' in error_json:
                            for msg in error_json['errorMessages']:
                                print(f"  Error message: {msg}")
                    except:
                        pass
                    return False
            elif response.status_code == 403:
                print(f"403 Permission denied to add users to group: {group_name}")
                # Get more details about the permission error
                print(f"Error details: {response.text[:500]}")
                return False
            elif response.status_code == 404:
                error_text = response.text
                print(f"404 error - Group not found: {group_name}")
                print(f"Error details: {error_text[:500]}")
                return False
            else:
                print(f"Unexpected response {response.status_code} for group {group_name}")
                print(f"Response body: {response.text[:500]}")
                return False
                
        except Exception as e:
            print(f"Exception while adding user to group {group_name}: {str(e)}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            return False
    
    def get_user_project_roles(self, user_email: str) -> List[Dict]:
        """Get all project roles for a user"""
        headers = self.get_auth_headers()
        if not headers:
            return []
        
        try:
            # Get user account ID
            user_info = self.check_user_exists(user_email)
            if not user_info.get('exists'):
                return []
            
            account_id = user_info['accountId']
            
            # Get all projects
            projects_response = requests.get(
                f"{self.base_url}/rest/api/3/project/search?expand=lead",
                headers=headers,
                timeout=30
            )
            
            if projects_response.status_code != 200:
                return []
            
            projects = projects_response.json().get('values', [])
            user_roles = []
            
            # Check each project for user's roles
            for project in projects:
                project_key = project['key']
                project_name = project['name']
                
                # Get project roles
                roles_response = requests.get(
                    f"{self.base_url}/rest/api/3/project/{project_key}/role",
                    headers=headers,
                    timeout=30
                )
                
                if roles_response.status_code == 200:
                    roles = roles_response.json()
                    
                    # Check each role for the user
                    for role_id, role_url in roles.items():
                        role_detail_response = requests.get(
                            role_url,
                            headers=headers,
                            timeout=30
                        )
                        
                        if role_detail_response.status_code == 200:
                            role_data = role_detail_response.json()
                            actors = role_data.get('actors', [])
                            
                            # Check if user is in this role
                            for actor in actors:
                                if actor.get('actorUser', {}).get('accountId') == account_id:
                                    user_roles.append({
                                        'project_key': project_key,
                                        'project_name': project_name,
                                        'role_name': role_data.get('name'),
                                        'role_id': role_id
                                    })
                                    break
            
            print(f"Found {len(user_roles)} project roles for {user_email}")
            return user_roles
            
        except Exception as e:
            print(f"Error getting project roles: {str(e)}")
            return []
    
    def add_user_to_project_role(self, user_email: str, project_key: str, role_id: str) -> bool:
        """Add user to a specific project role"""
        headers = self.get_auth_headers()
        if not headers:
            return False
        
        try:
            # Get user account ID
            user_info = self.check_user_exists(user_email)
            if not user_info.get('exists'):
                return False
            
            account_id = user_info['accountId']
            
            # Add user to project role
            response = requests.post(
                f"{self.base_url}/rest/api/3/project/{project_key}/role/{role_id}",
                headers=headers,
                json={'user': [account_id]},
                timeout=30
            )
            
            if response.status_code in [200, 201]:
                print(f"Added {user_email} to role {role_id} in project {project_key}")
                return True
            else:
                print(f"Failed to add to project role: {response.status_code}")
                return False
                
        except Exception as e:
            print(f"Error adding to project role: {str(e)}")
            return False
    
    def replicate_atlassian_access(self, source_user_email: str, target_user_email: str, target_display_name: str) -> Dict:
        """Replicate all Atlassian access from source to target user"""
        
        results = {
            'user_created': False,
            'groups_added': [],
            'groups_failed': [],
            'groups_skipped': [],
            'projects_added': [],
            'projects_failed': [],
            'source_user': source_user_email,
            'target_user': target_user_email,
            'summary': ''
        }
        
        try:
            print(f"Starting Atlassian access replication from {source_user_email} to {target_user_email}")
            
            # Step 1: Create target user if doesn't exist
            user_result = self.create_user(target_user_email, target_display_name)
            results['user_created'] = user_result.get('success', False)
            
            if not results['user_created']:
                results['summary'] = f"Failed to create user: {user_result.get('error', 'Unknown error')}"
                return results
            
            # IMPORTANT: Cache the account ID from creation result
            target_account_id = user_result.get('accountId')
            if not target_account_id:
                print("WARNING: No accountId returned from user creation, attempting to look up...")
                # Try to get the account ID
                target_user_info = self.check_user_exists(target_user_email)
                if target_user_info.get('exists'):
                    target_account_id = target_user_info.get('accountId')
                else:
                    print(f"ERROR: Cannot find account ID for {target_user_email}")
                    results['summary'] = "User created but cannot find account ID for group operations"
                    return results
            
            results['account_id'] = target_account_id
            print(f"Target user account ID: {target_account_id}")
            
            # Step 2: Get source user's groups with improved method
            print(f"Getting groups for source user: {source_user_email}")
            
            # First, ensure source user exists and get their account ID
            source_user_info = self.check_user_exists(source_user_email)
            if not source_user_info.get('exists'):
                print(f"Source user {source_user_email} not found in Atlassian")
                results['summary'] = f"Source user not found for group replication"
                return results
            
            source_account_id = source_user_info.get('accountId')
            print(f"Source user accountId: {source_account_id}")
            
            # Try multiple methods to get groups
            source_groups = []
            
            # Method 1: Direct API call
            try:
                headers = self.get_auth_headers()
                response = requests.get(
                    f"{self.base_url}/rest/api/3/user/groups?accountId={source_account_id}",
                    headers=headers,
                    timeout=30
                )
                
                if response.status_code == 200:
                    groups_data = response.json()
                    if groups_data:
                        source_groups = [{'name': g.get('name', g.get('groupName')), 'groupId': g.get('groupId')} for g in groups_data]
                        print(f"Found {len(source_groups)} groups via direct API")
                else:
                    print(f"Groups API returned {response.status_code}: {response.text[:200]}")
            except Exception as e:
                print(f"Error getting groups via API: {str(e)}")
            
            # Method 2: If no groups found, try alternative endpoint
            if not source_groups:
                try:
                    # Try getting all groups and checking membership
                    all_groups_response = requests.get(
                        f"{self.base_url}/rest/api/3/groups/picker?accountId={source_account_id}&maxResults=100",
                        headers=headers,
                        timeout=30
                    )
                    
                    if all_groups_response.status_code == 200:
                        picker_data = all_groups_response.json()
                        groups = picker_data.get('groups', [])
                        source_groups = [{'name': g.get('name'), 'groupId': g.get('groupId')} for g in groups]
                        print(f"Found {len(source_groups)} groups via picker API")
                except Exception as e:
                    print(f"Error getting groups via picker: {str(e)}")
            
            print(f"Total groups found for source user: {len(source_groups)}")
            
            # Step 3: Replicate group memberships using cached account ID
            if source_groups:
                # Groups to skip - admin groups and system groups that shouldn't be replicated
                skip_groups = [
                    'administrators',
                    'site-admins',
                    'jira-admins',
                    'confluence-admins',
                    'system-administrators',
                    'trusted-users',
                    'users',  # Default users group
                    'anyone',
                    'anonymous'
                ]
                
                # Also skip groups with certain patterns
                skip_patterns = [
                    '-admins',
                    '-administrators',
                    'admin-'
                ]
                
                for group in source_groups:
                    group_name = group.get('name')
                    if not group_name:
                        continue
                    
                    # Check if this is an admin/system group that should be skipped
                    group_lower = group_name.lower()
                    
                    # Skip if it's in the skip list
                    if group_lower in skip_groups:
                        results['groups_skipped'].append(group_name)
                        print(f"Skipping admin/system group: {group_name}")
                        continue
                    
                    # Skip if it matches admin patterns
                    if any(pattern in group_lower for pattern in skip_patterns):
                        results['groups_skipped'].append(group_name)
                        print(f"Skipping admin group: {group_name}")
                        continue
                    
                    # Skip JSM customer groups as they're already handled
                    if 'servicemanagement-customers' in group_lower:
                        results['groups_skipped'].append(group_name)
                        print(f"Skipping JSM customer group (already handled): {group_name}")
                        continue
                    
                    # Try to add user to the group using the CACHED account ID
                    if self.add_user_to_group_with_id(target_account_id, target_user_email, group_name):
                        results['groups_added'].append(group_name)
                        print(f" Added to group: {group_name}")
                    else:
                        results['groups_failed'].append(group_name)
                        print(f" Failed to add to group: {group_name}")
            else:
                print("WARNING: No groups found for source user - they may not be in any groups")
            
            # Step 4: Get source user's project roles (keeping existing logic)
            source_roles = self.get_user_project_roles(source_user_email)
            print(f"Found {len(source_roles)} project roles for source user")
            
            # Step 5: Replicate project roles
            for role in source_roles:
                role_desc = f"{role['project_name']} - {role['role_name']}"
                # Skip admin roles
                if 'admin' in role['role_name'].lower():
                    results['groups_skipped'].append(role_desc)
                    print(f"Skipping admin role: {role_desc}")
                    continue
                    
                if self.add_user_to_project_role(
                    target_user_email, 
                    role['project_key'], 
                    role['role_id']
                ):
                    results['projects_added'].append(role_desc)
                else:
                    results['projects_failed'].append(role_desc)
            
            # Generate summary
            results['summary'] = (
                f" User created/exists. "
                f"Groups: {len(results['groups_added'])} added, {len(results['groups_failed'])} failed, {len(results['groups_skipped'])} skipped. "
                f"Project roles: {len(results['projects_added'])} added, {len(results['projects_failed'])} failed."
            )
            
            print(f"Atlassian replication completed: {results['summary']}")
            return results
            
        except Exception as e:
            error_msg = f"Error in Atlassian replication: {str(e)}"
            print(error_msg)
            results['summary'] = error_msg
            results['error'] = str(e)
            return results
    
    def add_user_to_group_with_id(self, account_id: str, user_email: str, group_name: str) -> bool:
        """Add user to group using pre-fetched account ID"""
        headers = self.get_auth_headers()
        if not headers:
            print(f"No auth headers available for adding to group {group_name}")
            return False
        
        try:
            print(f"Attempting to add user {user_email} (ID: {account_id}) to group: {group_name}")
            
            # Skip membership check and directly try to add
            print(f"Sending POST request to add user to {group_name}")
            
            response = requests.post(
                f"{self.base_url}/rest/api/3/group/user",
                headers=headers,
                params={'groupname': group_name},
                json={'accountId': account_id},
                timeout=30
            )
            
            print(f"Add to group response for {group_name}: Status={response.status_code}")
            
            if response.status_code in [200, 201, 204]:
                print(f" Successfully added {user_email} to group: {group_name}")
                return True
            elif response.status_code == 400:
                error_text = response.text
                print(f"400 error for group {group_name}: {error_text[:500]}")
                
                error_lower = error_text.lower()
                if 'already a member' in error_lower or 'already in' in error_lower:
                    print(f"User already in group: {group_name}")
                    return True
                else:
                    return False
            else:
                print(f"Failed with status {response.status_code}: {response.text[:200]}")
                return False
                
        except Exception as e:
            print(f"Exception adding to group {group_name}: {str(e)}")
            return False

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

def get_secret(secret_name):
    """Retrieve secret from AWS Secrets Manager"""
    try:
        response = secrets_manager.get_secret_value(SecretId=secret_name)
        return json.loads(response['SecretString'])
    except ClientError as e:
        print(f"Error retrieving secret {secret_name}: {e}")
        return None

def get_ou_mapping():
    """Get OU mapping configuration from Secrets Manager"""
    return get_secret(OU_MAPPING_SECRET)

def get_dc_instance_id(domain=None, dc_host=None):
    """Dynamically find DC instance ID in production account"""
    _, ec2_prod_client = get_cross_account_clients()
    
    if dc_host and dc_host.startswith('i-'):
        try:
            response = ec2_prod_client.describe_instances(InstanceIds=[dc_host])
            if response['Reservations']:
                print(f"Using existing instance ID in prod account: {dc_host}")
                return dc_host
        except:
            print(f"Instance ID {dc_host} not found in prod account, searching for alternatives")
    
    if domain:
        try:
            domain_short = domain.split('.')[0]
            
            search_filters = [
                [
                    {'Name': 'tag:Domain', 'Values': [domain]},
                    {'Name': 'tag:Role', 'Values': ['DomainController']},
                    {'Name': 'instance-state-name', 'Values': ['running']}
                ],
                [
                    {'Name': 'tag:Name', 'Values': [f'*{domain_short}*dc*', f'*dc*{domain_short}*', '*DC*', '*domain*controller*']},
                    {'Name': 'instance-state-name', 'Values': ['running']}
                ]
            ]
            
            for filters in search_filters:
                response = ec2_prod_client.describe_instances(Filters=filters)
                
                for reservation in response['Reservations']:
                    for instance in reservation['Instances']:
                        instance_id = instance['InstanceId']
                        ssm_prod_client, _ = get_cross_account_clients()
                        try:
                            ssm_info = ssm_prod_client.describe_instance_information(
                                Filters=[{'Key': 'InstanceIds', 'Values': [instance_id]}]
                            )
                            if ssm_info['InstanceInformationList']:
                                print(f"Found DC instance {instance_id} in prod account for domain {domain}")
                                return instance_id
                        except:
                            continue
                        
        except Exception as e:
            print(f"Error finding DC instance in prod account: {str(e)}")
    
    try:
        ssm_prod_client, _ = get_cross_account_clients()
        
        print("Searching for Windows instances with SSM in production account...")
        
        ssm_instances = ssm_prod_client.describe_instance_information(
            Filters=[
                {'Key': 'PlatformTypes', 'Values': ['Windows']}
            ]
        )
        
        for instance in ssm_instances['InstanceInformationList']:
            instance_id = instance['InstanceId']
            print(f"Found Windows instance with SSM in prod: {instance_id}")
            return instance_id
                        
    except Exception as e:
        print(f"Error in prod account DC search: {str(e)}")
    
    raise ValueError("No suitable Domain Controller instance found in production account")

def determine_ou_and_domain(employee_data, ou_mapping):
    """Determine the correct OU and domain based on employee data"""
    
    department = employee_data.get('department', '').lower()
    work_location = employee_data.get('workLocation', '').lower()
    company = employee_data.get('company', '').lower()
    
    for rule in ou_mapping['rules']:
        conditions = rule.get('conditions', {})
        
        if 'departments' in conditions:
            if any(dept.lower() in department for dept in conditions['departments']):
                dc_instance_id = get_dc_instance_id(rule['domain'], rule.get('dc_host'))
                return (rule['ou'], rule['domain'], dc_instance_id, 
                       rule.get('netbios_domain', rule['domain'].split('.')[0].upper()))
        
        if 'locations' in conditions:
            if any(loc.lower() in work_location for loc in conditions['locations']):
                dc_instance_id = get_dc_instance_id(rule['domain'], rule.get('dc_host'))
                return (rule['ou'], rule['domain'], dc_instance_id,
                       rule.get('netbios_domain', rule['domain'].split('.')[0].upper()))
        
        if 'keywords' in conditions:
            all_text = f"{department} {work_location} {company} {employee_data.get('fullName', '')}".lower()
            if any(keyword.lower() in all_text for keyword in conditions['keywords']):
                dc_instance_id = get_dc_instance_id(rule['domain'], rule.get('dc_host'))
                return (rule['ou'], rule['domain'], dc_instance_id,
                       rule.get('netbios_domain', rule['domain'].split('.')[0].upper()))
    
    default = ou_mapping.get('default', {})
    dc_instance_id = get_dc_instance_id(default.get('domain'), default.get('dc_host'))
    return (default.get('ou'), default.get('domain'), dc_instance_id,
           default.get('netbios_domain', default.get('domain', '').split('.')[0].upper()))

def execute_ps_script(script, instance_id):
    """Execute PowerShell script via SSM in production account"""
    try:
        ssm_prod_client, _ = get_cross_account_clients()
        
        print(f"Executing PowerShell script on instance: {instance_id} in prod account")
        
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
            print(f"Command failed with output: {result.get('StandardOutputContent', '')}")
            print(f"Error output: {error_output}")
            raise Exception(f"Command failed: {error_output}")
            
    except Exception as e:
        print(f"Error executing PowerShell script: {str(e)}")
        raise

def generate_email(first_name, last_name, domain):
    """Generate email address based on format and domain"""
    first_name = first_name.lower().strip()
    last_name = last_name.lower().strip()
    
    first_name = ''.join(c for c in first_name if c.isalnum())
    last_name = ''.join(c for c in last_name if c.isalnum())
    
    if EMAIL_FORMAT == "firstinitial.lastname":
        if first_name:
            email = f"{first_name[0]}.{last_name}@{domain}"
        else:
            email = f"{last_name}@{domain}"
    else:
        email = f"{first_name}.{last_name}@{domain}"
    
    return email

def generate_username(email):
    """Generate username from email"""
    username = email.split('@')[0]
    return username[:20]

def get_domain_credentials(domain, default_credentials):
    """Get domain-specific credentials if configured, with proper fallback"""
    # First check if default credentials exist
    if not default_credentials:
        print(f"Warning: No default AD credentials found")
        return None
    
    # Try to get domain-specific credentials
    try:
        domain_prefix = domain.split('.')[0].lower()
        domain_secret_name = f"{AD_CREDENTIALS_SECRET}-{domain_prefix}"
        print(f"Checking for domain-specific secret: {domain_secret_name}")
        
        domain_secret = get_secret(domain_secret_name)
        if domain_secret:
            print(f"Using domain-specific credentials for {domain}")
            # Ensure proper username format for the domain
            if 'username' in domain_secret and '\\' not in domain_secret['username']:
                # Add domain prefix if not present
                netbios_domain = domain.split('.')[0].upper()
                domain_secret['username'] = f"{netbios_domain}\\{domain_secret['username']}"
            return domain_secret
    except Exception as e:
        print(f"No domain-specific credentials found for {domain}, using default")
    
    # Use default credentials but ensure proper format
    if default_credentials:
        # Check if username needs domain prefix
        if 'username' in default_credentials:
            username = default_credentials['username']
            # If username doesn't have domain prefix, add it
            if '\\' not in username and '@' not in username:
                netbios_domain = 'YOUR_COMPANY_NAME'  # Default to YOUR_COMPANY_NAME based on your setup
                default_credentials['username'] = f"{netbios_domain}\\{username}"
                print(f"Updated username format to: {default_credentials['username']}")
            elif '\\' in username:
                # Ensure the domain prefix is correct
                parts = username.split('\\')
                if len(parts) == 2 and parts[0].lower() == 'aws':
                    # Fix incorrect 'aws\' prefix
                    default_credentials['username'] = f"YOUR_COMPANY_NAME\\{parts[1]}"
                    print(f"Fixed username format from aws\\ to: {default_credentials['username']}")
    
    return default_credentials

def find_user_in_ad(search_name, domain, dc_host):
    """Find user by name, email, or username"""
    
    if not search_name:
        return None
    
    ps_script = f"""
    Import-Module ActiveDirectory
    
    $searchTerms = '{search_name}'
    
    $user = Get-ADUser -Filter "Name -eq '$searchTerms' -or DisplayName -eq '$searchTerms' -or SamAccountName -eq '$searchTerms' -or EmailAddress -eq '$searchTerms'" `
                      -Properties DisplayName, EmailAddress, SamAccountName -ErrorAction SilentlyContinue | 
                      Select-Object -First 1
    
    if ($user) {{
        Write-Output "USER_FOUND: $($user.SamAccountName)"
        Write-Output "USER_NAME: $($user.Name)"
        Write-Output "USER_EMAIL: $($user.EmailAddress)"
    }} else {{
        Write-Output "USER_NOT_FOUND"
    }}
    """
    
    try:
        result = execute_ps_script(ps_script, dc_host)
        
        user_info = {}
        for line in result.split('\n'):
            if line.startswith('USER_FOUND:'):
                user_info['username'] = line.split(':', 1)[1].strip()
            elif line.startswith('USER_NAME:'):
                user_info['name'] = line.split(':', 1)[1].strip()
            elif line.startswith('USER_EMAIL:'):
                user_info['email'] = line.split(':', 1)[1].strip()
        
        return user_info if user_info else None
    except Exception as e:
        print(f"Error finding user in AD: {str(e)}")
        return None

def trigger_ad_sync(domain, dc_instance_id):
    """Trigger AD Connect sync for domain"""
    
    ps_script = """
    try {
        Start-ADSyncSyncCycle -PolicyType Delta
        Write-Output "AD sync triggered successfully"
    } catch {
        Write-Output "Warning: Could not trigger AD sync: $_"
    }
    """
    
    try:
        result = execute_ps_script(ps_script, dc_instance_id)
        return True
    except Exception as e:
        print(f"Error triggering AD sync: {str(e)}")
        return False

def process_microsoft_365_integration_enhanced(user_email, source_user_identifier=None):
    """Handle Microsoft 365 license assignment and complete access replication"""
    
    m365_manager = Microsoft365Manager()
    
    try:
        # Quick check if user exists in Azure AD
        user_exists = m365_manager.check_user_exists(user_email)
        
        if not user_exists:
            return {
                'user_synced': False,
                'license_assigned': False,
                'access_replicated': False,
                'errors': [f'User {user_email} not found in Azure AD']
            }
        
        m365_results = {
            'user_synced': True,
            'license_assigned': False,
            'access_replicated': False,
            'errors': []
        }
        
        # Assign Business Premium license
        try:
            # First set usage location
            location_set = m365_manager.set_user_usage_location(user_email, 'GB')
            if not location_set:
                m365_results['errors'].append("Failed to set usage location")
            
            license_result = m365_manager.assign_license_to_user(user_email)
            m365_results['license_assigned'] = True
            m365_results['license_info'] = license_result
            print(f"Successfully assigned license to {user_email}")
        except Exception as e:
            error_msg = f"Failed to assign license: {str(e)}"
            m365_results['errors'].append(error_msg)
            print(error_msg)
        
        # Replicate access if source user provided
        if source_user_identifier:
            try:
                replication_result = m365_manager.replicate_m365_access(
                    source_user_identifier, 
                    user_email
                )
                m365_results['access_replicated'] = True
                m365_results['replication_info'] = replication_result
                print(f"Successfully replicated M365 access from {source_user_identifier}")
            except Exception as e:
                error_msg = f"Failed to replicate M365 access: {str(e)}"
                m365_results['errors'].append(error_msg)
                print(error_msg)
        
        return m365_results
        
    except Exception as e:
        error_msg = f"M365 integration error: {str(e)}"
        return {
            'user_synced': False,
            'license_assigned': False,
            'access_replicated': False,
            'errors': [error_msg]
        }

def process_atlassian_integration(user_email: str, display_name: str, source_user_identifier: Optional[str] = None) -> Dict:
    """Process Atlassian account creation and access replication"""
    
    # Check if Atlassian is enabled
    if not ATLASSIAN_ENABLED:
        print("Atlassian integration is disabled")
        return {
            'enabled': False,
            'message': 'Atlassian integration is disabled'
        }
    
    try:
        atlassian_manager = AtlassianManager()
        
        # Check if credentials are available
        if not atlassian_manager.get_credentials():
            print("Atlassian credentials not available")
            return {
                'enabled': False,
                'message': 'Atlassian credentials not configured'
            }
        
        atlassian_results = {
            'enabled': True,
            'account_created': False,
            'access_replicated': False,
            'details': {}
        }
        
        # If source user is provided, do full replication
        if source_user_identifier:
            # Determine source user email
            source_email = source_user_identifier
            if '@' not in source_email:
                # If just a name, try to construct email
                domain = user_email.split('@')[1]
                source_email = f"{source_user_identifier.replace(' ', '.').lower()}@{domain}"
            
            # Replicate access (this also creates the user)
            replication_results = atlassian_manager.replicate_atlassian_access(
                source_email,
                user_email,
                display_name
            )
            
            atlassian_results['account_created'] = replication_results.get('user_created', False)
            atlassian_results['access_replicated'] = len(replication_results.get('groups_added', [])) > 0
            atlassian_results['details'] = replication_results
            
        else:
            # Just create the user without replication
            create_result = atlassian_manager.create_user(user_email, display_name)
            atlassian_results['account_created'] = create_result.get('success', False)
            atlassian_results['details'] = create_result
        
        return atlassian_results
        
    except Exception as e:
        error_msg = f"Atlassian integration error: {str(e)}"
        print(error_msg)
        return {
            'enabled': True,
            'account_created': False,
            'access_replicated': False,
            'error': error_msg
        }

def handle_delayed_m365_and_atlassian_processing(sqs_message):
    """Handle M365 and Atlassian processing from delayed SQS message"""
    
    try:
        message_data = json.loads(sqs_message['body'])
        
        user_email = message_data['user_email']
        ticket_key = message_data['ticket_key']
        source_user_identifier = message_data.get('source_user_identifier')
        retry_count = message_data.get('retry_count', 0)
        employee_data = message_data.get('employee_data', {})
        
        print(f"Processing delayed M365 and Atlassian integration for {user_email} (retry #{retry_count})")
        
        # Process M365 integration
        m365_results = process_microsoft_365_integration_enhanced(user_email, source_user_identifier)
        
        # Process Atlassian integration if enabled
        atlassian_results = None
        if ATLASSIAN_ENABLED:
            display_name = employee_data.get('fullName', f"{employee_data.get('firstName', '')} {employee_data.get('lastName', '')}")
            atlassian_results = process_atlassian_integration(
                user_email, 
                display_name, 
                source_user_identifier
            )
        
        # Update Jira with results
        if ticket_key:
            if m365_results.get('user_synced'):
                success_message = f""" **Microsoft 365 Integration Completed Successfully!**

**User:** {user_email}
**Status:** User synced to Azure AD and license assigned
**License:** {" Assigned" if m365_results.get('license_assigned') else " Failed"}
**Groups Replicated:** {" Completed" if m365_results.get('access_replicated') else "N/A"}"""
                
                if m365_results.get('replication_info'):
                    rep_info = m365_results['replication_info']
                    groups_count = len(rep_info.get('groups_added', []))
                    groups_skipped = len(rep_info.get('groups_skipped', []))
                    if groups_count > 0:
                        success_message += f"\n**Groups Added:** {groups_count}"
                        success_message += f"\n  • " + "\n  • ".join(rep_info['groups_added'][:5])
                        if groups_count > 5:
                            success_message += f"\n  • ... and {groups_count - 5} more"
                    if groups_skipped > 0:
                        success_message += f"\n**Groups Skipped:** {groups_skipped} (mail-enabled/system groups)"
                
                if atlassian_results and atlassian_results.get('enabled'):
                    success_message += f"""

**Atlassian Integration:**
**Account:** {" Created/Exists" if atlassian_results.get('account_created') else " Failed"}"""
                    
                    if atlassian_results.get('details'):
                        details = atlassian_results['details']
                        groups_count = len(details.get('groups_added', []))
                        projects_count = len(details.get('projects_added', []))
                        if groups_count > 0:
                            success_message += f"\n**Groups Added:** {groups_count}"
                            success_message += f"\n  • " + "\n  • ".join(details['groups_added'][:3])
                        if projects_count > 0:
                            success_message += f"\n**Project Roles Added:** {projects_count}"
                            success_message += f"\n  • " + "\n  • ".join(details['projects_added'][:3])
                
                if m365_results.get('errors') or (atlassian_results and atlassian_results.get('error')):
                    success_message += f"\n\n **Issues Encountered:**"
                    for error in m365_results.get('errors', []):
                        success_message += f"\n- M365: {error}"
                    if atlassian_results and atlassian_results.get('error'):
                        success_message += f"\n- Atlassian: {atlassian_results['error']}"
                
                update_jira_ticket(ticket_key, success_message, success=True)
            else:
                # User still not synced, schedule retry if not exceeded max retries
                if retry_count < 3:  # Max 3 retries
                    print(f"User not synced yet, scheduling retry #{retry_count + 1}")
                    message_data['retry_count'] = retry_count + 1
                    
                    # Schedule retry with shorter delay (5 minutes)
                    sqs.send_message(
                        QueueUrl=M365_DELAY_QUEUE_URL,
                        MessageBody=json.dumps(message_data),
                        DelaySeconds=300  # 5 minutes
                    )
                    
                    update_jira_ticket(
                        ticket_key,
                        f"⏳ M365 integration retry #{retry_count + 1} scheduled. User {user_email} not yet synced to Azure AD.",
                        success=True
                    )
                else:
                    # Max retries exceeded
                    failure_message = f""" **Microsoft 365 Integration Failed**

**User:** {user_email}
**Issue:** User did not sync to Azure AD after multiple attempts
**Retries:** {retry_count}

**Manual Action Required:**
1. Check AD Connect sync status
2. Verify user exists in on-premises AD
3. Manually assign Microsoft 365 license via admin center
4. Check for any AD Connect sync errors"""
                    
                    update_jira_ticket(ticket_key, failure_message, success=False)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'success': True,
                'processed': 'delayed_m365_atlassian_integration',
                'user_email': user_email,
                'm365_results': m365_results,
                'atlassian_results': atlassian_results
            })
        }
        
    except Exception as e:
        error_msg = f"Error in delayed processing: {str(e)}"
        print(error_msg)
        
        # Update Jira with error if ticket_key available
        if 'ticket_key' in message_data:
            update_jira_ticket(
                message_data['ticket_key'],
                f" Integration failed during delayed processing: {error_msg}",
                success=False
            )
        
        return {
            'statusCode': 500,
            'body': json.dumps({
                'success': False,
                'error': error_msg
            })
        }

def update_jira_ticket(ticket_key, message, success=True):
    """Update Jira ticket with comment"""
    
    if not ticket_key:
        print("No ticket key provided for Jira update")
        return
    
    if ticket_key.startswith('TEST-'):
        print(f"Skipping Jira update for test ticket: {ticket_key}")
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
        
        if isinstance(message, dict) and success:
            formatted_message = f""" AD account created successfully!

**Account Details:**
- Username: {message.get('username')}
- Email: {message.get('email')}
- Domain: {message.get('domain')}
- OU: {message.get('ou', 'N/A')}"""

            if message.get('access_replicated_from'):
                replication_summary = message.get('replication_summary', {})
                formatted_message += f"""

**Access Replicated From:** {message['access_replicated_from']}
- Groups Copied: {len(replication_summary.get('groups_copied', []))}
- Groups List: {', '.join(replication_summary.get('groups_copied', [])) if replication_summary.get('groups_copied') else 'None'}"""
                
                if message.get('replication_warning'):
                    formatted_message += f"\n\n Warning: {message['replication_warning']}"
        else:
            formatted_message = str(message)
        
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
                                "text": formatted_message
                            }
                        ]
                    }
                ]
            }
        }
        
        import urllib3
        http = urllib3.PoolManager()
        
        response = http.request(
            'POST',
            f"{JIRA_URL}/rest/api/3/issue/{ticket_key}/comment",
            body=json.dumps(comment_body),
            headers=headers
        )
        
        if response.status != 201:
            print(f"Failed to update Jira ticket: {response.status} - {response.data}")
    except Exception as e:
        print(f"Error updating Jira ticket: {str(e)}")

def send_error_notification(error_message, ticket_key):
    """Send error notification via SNS"""
    
    try:
        message = f"""Error in Employee Onboarding Automation

Ticket: {ticket_key}
Error: {error_message}
Time: {datetime.now().isoformat()}

Please check CloudWatch logs for more details."""
        
        sns.publish(
            TopicArn=ERROR_TOPIC_ARN,
            Subject=f"Onboarding Error - Ticket {ticket_key}",
            Message=message
        )
    except Exception as e:
        print(f"Error sending notification: {str(e)}")

def create_ad_user(employee_data, ad_credentials):
    """Create AD user in the appropriate domain"""
    
    ou_mapping = get_ou_mapping()
    target_ou, email_domain, dc_host, netbios_domain = determine_ou_and_domain(employee_data, ou_mapping)
    
    if not target_ou or not email_domain:
        raise ValueError("Could not determine target OU or domain for user")
    
    print(f"Creating user in Domain: {email_domain}, OU: {target_ou}, DC: {dc_host}")
    
    email = generate_email(employee_data['firstName'], employee_data['lastName'], email_domain)
    username = generate_username(email)
    
    # Get proper credentials for the domain
    domain_creds = get_domain_credentials(email_domain, ad_credentials)
    
    if not domain_creds:
        raise ValueError("No AD credentials available for domain operations")
    
    # Ensure username has proper format
    cred_username = domain_creds['username']
    cred_password = domain_creds['password']
    
    ps_script = f"""
    $ErrorActionPreference = 'Stop'
    
    Import-Module ActiveDirectory
    
    $password = ConvertTo-SecureString '{cred_password}' -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential ('{cred_username}', $password)
    
    $userPassword = -join ((65..90) + (97..122) + (48..57) + (33,35,36,37,38,42,43,45,46,47,58,59,61,63,64,91,93,94,95,123,125,126) | Get-Random -Count 16 | ForEach-Object {{[char]$_}})
    $securePassword = ConvertTo-SecureString $userPassword -AsPlainText -Force
    
    Write-Output "Target Domain: {email_domain}"
    Write-Output "NetBIOS Domain: {netbios_domain}"
    Write-Output "Target OU: {target_ou}"
    Write-Output "Using credentials: {cred_username}"
    
    try {{
        $dc = [System.DirectoryServices.ActiveDirectory.Domain]::GetCurrentDomain().DomainControllers[0].Name
        Write-Output "Using Domain Controller: $dc"
    }} catch {{
        $dc = "$env:COMPUTERNAME.$env:USERDNSDOMAIN"
        Write-Output "Using fallback DC: $dc"
    }}
    
    try {{
        $existingUser = Get-ADUser -Filter "SamAccountName -eq '{username}' -or UserPrincipalName -eq '{email}'" `
                                  -Server $dc -Credential $credential -ErrorAction SilentlyContinue
        
        if ($existingUser) {{
            Write-Output "ERROR: User {username} already exists in domain {email_domain}"
            exit 1
        }}
    }} catch {{
        Write-Output "User {username} does not exist, proceeding with creation"
    }}
    
    $targetPath = '{target_ou}'
    try {{
        $ouExists = Get-ADOrganizationalUnit -Identity '{target_ou}' -Server $dc -Credential $credential -ErrorAction Stop
        Write-Output "OU verified: $($ouExists.DistinguishedName)"
    }} catch {{
        Write-Output "WARNING: Target OU '{target_ou}' not found, looking for alternatives..."
        
        try {{
            $availableOUs = Get-ADOrganizationalUnit -Filter * -Server $dc -Credential $credential | 
                           Where-Object {{ $_.Name -like '*User*' -or $_.Name -like '*Employee*' }} | 
                           Select-Object -First 1
            
            if ($availableOUs) {{
                $targetPath = $availableOUs.DistinguishedName
                Write-Output "Using alternative OU: $targetPath"
            }} else {{
                $targetPath = "CN=Users,DC={email_domain.replace('.', ',DC=')}"
                Write-Output "Using Users container: $targetPath"
            }}
        }} catch {{
            $targetPath = "DC={email_domain.replace('.', ',DC=')}"
            Write-Output "Using domain root: $targetPath"
        }}
    }}
    
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
        Path = $targetPath
        Server = $dc
        Credential = $credential
    }}
    
    if ('{employee_data.get('jobTitle', '')}') {{ $userParams.Title = '{employee_data.get('jobTitle', '')}' }}
    if ('{employee_data.get('department', '')}') {{ $userParams.Department = '{employee_data.get('department', '')}' }}
    if ('{employee_data.get('workLocation', '')}') {{ $userParams.Office = '{employee_data.get('workLocation', '')}' }}
    
    switch ('{email_domain}') {{
        'YOUR_COMPANY_NAME.COM' {{ $userParams.Company = 'YOUR_COMPANY_NAME' }}
        'YOUR_COMPANY_NAME.COM' {{ $userParams.Company = 'YOUR_COMPANY_NAME' }}
        'YOUR_COMPANY_NAME.COM' {{ $userParams.Company = 'YOUR_COMPANY_NAME' }}
        default {{ $userParams.Company = '{email_domain}'.Split('.')[0] }}
    }}
    
    if ('{employee_data.get('manager', '')}') {{
        $managerName = '{employee_data.get('manager', '')}'
        $manager = Get-ADUser -Filter "Name -eq '$managerName' -or DisplayName -eq '$managerName'" `
                             -Server $dc -Credential $credential -ErrorAction SilentlyContinue | 
                   Select-Object -First 1
        if ($manager) {{
            $userParams.Manager = $manager.DistinguishedName
            Write-Output "Manager found: $($manager.Name)"
        }} else {{
            Write-Output "Manager not found: $managerName"
        }}
    }}
    
    try {{
        New-ADUser @userParams
        Write-Output "SUCCESS: Created user {username} with email {email} in domain {email_domain}"
        Write-Output "TEMPPASS: $userPassword"
        Write-Output "DOMAIN: {email_domain}"
        Write-Output "NETBIOS: {netbios_domain}"
        Write-Output "OU: $targetPath"
    }} catch {{
        Write-Output "ERROR: Failed to create user: $_"
        exit 1
    }}
    """
    
    try:
        result = execute_ps_script(ps_script, dc_host)
        
        output_text = result
        result_data = {
            'success': True,
            'username': username,
            'email': email,
            'domain': email_domain,
            'dc_host': dc_host,
            'message': f"User {username} created successfully"
        }
        
        for line in output_text.split('\n'):
            if line.startswith('TEMPPASS:'):
                result_data['tempPassword'] = line.split(':', 1)[1].strip()
            elif line.startswith('OU:'):
                result_data['ou'] = line.split(':', 1)[1].strip()
        
        copy_from_user = employee_data.get('copyAccessFrom') or employee_data.get('replicateAccessFrom')
        
        if copy_from_user and result_data['success']:
            print(f"Attempting to replicate access from: {copy_from_user}")
            
            source_user_info = find_user_in_ad(copy_from_user, email_domain, dc_host)
            
            if source_user_info:
                print(f"Found source user: {source_user_info['username']}")
                
                replication_result = replicate_user_access(
                    source_user_info['username'],
                    result_data['username'],
                    email_domain,
                    dc_host,
                    domain_creds  # Pass credentials to replication function
                )
                
                result_data['access_replicated_from'] = copy_from_user
                result_data['groups'] = replication_result['groups_copied']
                result_data['replication_summary'] = replication_result
                
                print(f"Access replication completed. Groups copied: {len(replication_result['groups_copied'])}")
            else:
                print(f"Warning: Could not find source user '{copy_from_user}' for access replication")
                result_data['replication_warning'] = f"Could not find user '{copy_from_user}' to copy access from"
        
        return result_data
            
    except Exception as e:
        print(f"Error creating AD user: {str(e)}")
        raise

def replicate_user_access(source_username, target_username, domain, dc_host, credentials=None):
    """Replicate all access from source user to target user with proper credentials"""
    
    # Use provided credentials or get them
    if not credentials:
        credentials = get_domain_credentials(domain, get_secret(AD_CREDENTIALS_SECRET))
    
    if not credentials:
        print("Warning: No credentials available for access replication")
        return {
            'source_user': source_username,
            'target_user': target_username,
            'groups_copied': [],
            'groups_failed': [],
            'success': False,
            'error': 'No credentials available'
        }
    
    cred_username = credentials['username']
    cred_password = credentials['password']
    
    ps_script = f"""
    $ErrorActionPreference = 'Continue'
    Import-Module ActiveDirectory
    
    # Set up credentials
    $password = ConvertTo-SecureString '{cred_password}' -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential ('{cred_username}', $password)
    
    Write-Output "Using credentials: {cred_username}"
    
    try {{
        # Get domain controller
        try {{
            $dc = [System.DirectoryServices.ActiveDirectory.Domain]::GetCurrentDomain().DomainControllers[0].Name
            Write-Output "Using DC: $dc"
        }} catch {{
            $dc = "$env:COMPUTERNAME.$env:USERDNSDOMAIN"
            Write-Output "Using fallback DC: $dc"
        }}
        
        # Search for source user more thoroughly
        Write-Output "Searching for source user: {source_username}"
        $sourceUser = $null
        
        # Try multiple search methods
        try {{
            $sourceUser = Get-ADUser -Identity '{source_username}' -Properties MemberOf, Department, Title, Manager, Office -Server $dc -Credential $credential -ErrorAction Stop
            Write-Output "Found user by Identity: $($sourceUser.SamAccountName)"
        }} catch {{
            Write-Output "Identity search failed, trying filter search..."
            $sourceUser = Get-ADUser -Filter "SamAccountName -eq '{source_username}' -or UserPrincipalName -eq '{source_username}' -or EmailAddress -eq '{source_username}'" -Properties MemberOf, Department, Title, Manager, Office -Server $dc -Credential $credential -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($sourceUser) {{
                Write-Output "Found user by Filter: $($sourceUser.SamAccountName)"
            }}
        }}
        
        if (-not $sourceUser) {{
            Write-Output "ERROR: Source user {source_username} not found in AD"
            Write-Output "COPIED_GROUPS: "
            Write-Output "FAILED_GROUPS: "
            exit 1
        }}
        
        Write-Output "SOURCE_USER_FOUND: $($sourceUser.Name)"
        Write-Output "SOURCE_USER_DN: $($sourceUser.DistinguishedName)"
        
        # Get all groups of source user
        Write-Output "Getting group memberships..."
        if ($sourceUser.MemberOf) {{
            Write-Output "MemberOf property contains $($sourceUser.MemberOf.Count) groups"
            $sourceGroups = @()
            foreach ($groupDN in $sourceUser.MemberOf) {{
                try {{
                    Write-Output "Processing group DN: $groupDN"
                    $group = Get-ADGroup -Identity $groupDN -Server $dc -Credential $credential -ErrorAction SilentlyContinue
                    if ($group) {{
                        # Skip default groups and system groups
                        if ($group.Name -notin @('Domain Users', 'Authenticated Users', 'Everyone')) {{
                            # Check if it's a security group (not distribution)
                            if ($group.GroupCategory -eq 'Security') {{
                                $sourceGroups += $group
                                Write-Output "Added security group: $($group.Name) (Category: $($group.GroupCategory), Scope: $($group.GroupScope))"
                            }} else {{
                                Write-Output "Skipped distribution group: $($group.Name) (Category: $($group.GroupCategory))"
                            }}
                        }} else {{
                            Write-Output "Skipped system group: $($group.Name)"
                        }}
                    }} else {{
                        Write-Output "Could not resolve group DN: $groupDN"
                    }}
                }} catch {{
                    Write-Output "Error processing group DN '$groupDN': $_"
                }}
            }}
        }} else {{
            Write-Output "No MemberOf property found, trying Get-ADPrincipalGroupMembership..."
            try {{
                $allGroups = Get-ADPrincipalGroupMembership -Identity $sourceUser -Server $dc -Credential $credential
                $sourceGroups = $allGroups | Where-Object {{ 
                    $_.Name -notin @('Domain Users', 'Authenticated Users', 'Everyone') -and
                    $_.GroupCategory -eq 'Security'
                }}
                Write-Output "Found $($sourceGroups.Count) security groups via Get-ADPrincipalGroupMembership"
                foreach ($group in $sourceGroups) {{
                    Write-Output "Found group: $($group.Name) (Category: $($group.GroupCategory), Scope: $($group.GroupScope))"
                }}
            }} catch {{
                Write-Output "Get-ADPrincipalGroupMembership failed: $_"
                $sourceGroups = @()
            }}
        }}
        
        Write-Output "GROUPS_COUNT: $($sourceGroups.Count)"
        
        if ($sourceGroups.Count -eq 0) {{
            Write-Output "WARNING: No groups found for source user"
            Write-Output "COPIED_GROUPS: "
            Write-Output "FAILED_GROUPS: "
            Write-Output "SUCCESS: Access replicated from {source_username} to {target_username} (0 groups)"
            exit 0
        }}
        
        # Get target user
        Write-Output "Getting target user: {target_username}"
        $targetUser = Get-ADUser -Identity '{target_username}' -Server $dc -Credential $credential -ErrorAction Stop
        Write-Output "TARGET_USER_FOUND: $($targetUser.Name)"
        
        # Copy AD group memberships
        $copiedGroups = @()
        $failedGroups = @()
        $skippedGroups = @()
        
        foreach ($group in $sourceGroups) {{
            Write-Output "Attempting to add to group: $($group.Name) (DN: $($group.DistinguishedName))"
            try {{
                # Check if group allows member addition
                $groupInfo = Get-ADGroup -Identity $group.DistinguishedName -Properties GroupCategory, GroupScope -Server $dc -Credential $credential
                
                if ($groupInfo.GroupCategory -eq 'Security') {{
                    Add-ADGroupMember -Identity $group.DistinguishedName -Members $targetUser.DistinguishedName -Server $dc -Credential $credential -ErrorAction Stop
                    $copiedGroups += $group.Name
                    Write-Output "SUCCESS: Added to security group: $($group.Name)"
                }} else {{
                    $skippedGroups += $group.Name
                    Write-Output "SKIPPED: Cannot add to distribution group: $($group.Name)"
                }}
            }} catch {{
                if ($_.Exception.Message -like "*already a member*") {{
                    $copiedGroups += $group.Name
                    Write-Output "Already member of: $($group.Name)"
                }} elseif ($_.Exception.Message -like "*mail-enabled*" -or $_.Exception.Message -like "*distribution*") {{
                    $skippedGroups += $group.Name
                    Write-Output "SKIPPED: Mail-enabled/distribution group: $($group.Name)"
                }} else {{
                    $failedGroups += $group.Name
                    Write-Output "FAILED to add to group: $($group.Name) - $_"
                }}
            }}
        }}
        
        Write-Output "COPIED_GROUPS: $($copiedGroups -join ',')"
        Write-Output "FAILED_GROUPS: $($failedGroups -join ',')"
        Write-Output "SKIPPED_GROUPS: $($skippedGroups -join ',')"
        Write-Output "SUCCESS: Access replicated from {source_username} to {target_username}"
        
    }} catch {{
        Write-Output "ERROR: $_"
        Write-Output "COPIED_GROUPS: "
        Write-Output "FAILED_GROUPS: "
        throw
    }}
    """
    
    try:
        result = execute_ps_script(ps_script, dc_host)
        
        replication_summary = {
            'source_user': source_username,
            'target_user': target_username,
            'groups_copied': [],
            'groups_failed': [],
            'success': False
        }
        
        for line in result.split('\n'):
            if line.startswith('COPIED_GROUPS:'):
                groups = line.split(':', 1)[1].strip()
                if groups:
                    replication_summary['groups_copied'] = [g.strip() for g in groups.split(',') if g.strip()]
            elif line.startswith('FAILED_GROUPS:'):
                groups = line.split(':', 1)[1].strip()
                if groups:
                    replication_summary['groups_failed'] = [g.strip() for g in groups.split(',') if g.strip()]
            elif line.startswith('SUCCESS:'):
                replication_summary['success'] = True
        
        return replication_summary
    except Exception as e:
        print(f"Error replicating access: {str(e)}")
        return {
            'source_user': source_username,
            'target_user': target_username,
            'groups_copied': [],
            'groups_failed': [],
            'success': False,
            'error': str(e)
        }

def lambda_handler(event, context):
    """Main Lambda handler - supports both SNS (new users) and SQS (delayed M365/Atlassian) events"""
    
    event_type = determine_event_type(event)
    
    if event_type == 'sqs_delayed_m365':
        # Handle delayed M365 and Atlassian processing
        return handle_delayed_m365_and_atlassian_processing(event['Records'][0])
    
    elif event_type == 'sns_onboarding':
        # Handle new user onboarding
        ticket_key = None
        
        try:
            # Parse SNS message - handle both formats
            sns_record = event['Records'][0]['Sns']
            message_content = sns_record.get('Message', '')
            
            # Check if it's a JSON string or key-value pairs from Jira
            try:
                # First try to parse as direct JSON
                sns_message = json.loads(message_content)
                
                # Check if it's wrapped in automationData (Jira format)
                if 'automationData' in sns_message:
                    if 'default' in sns_message['automationData']:
                        # Parse the nested JSON string
                        sns_message = json.loads(sns_message['automationData']['default'])
                
            except:
                # If that fails, check if it's from Jira automation with MessageAttributes
                if 'MessageAttributes' in sns_record:
                    # Jira sends data in MessageAttributes with a 'default' key
                    default_attr = sns_record['MessageAttributes'].get('default', {})
                    if default_attr and 'Value' in default_attr:
                        sns_message = json.loads(default_attr['Value'])
                    else:
                        # Try to build from individual attributes
                        sns_message = {}
                        for key, value in sns_record['MessageAttributes'].items():
                            if 'Value' in value:
                                sns_message[key] = value['Value']
                else:
                    # Last resort - try to parse the message itself
                    sns_message = {'error': 'Could not parse message', 'raw': message_content}
            
            print(f"Parsed SNS message: {json.dumps(sns_message)}")
            
            ticket_key = sns_message.get('ticketKey')
            employee_data = sns_message.get('employeeData', {})
            
            # Handle firstName/lastName if only fullName is provided
            if 'fullName' in employee_data and ('firstName' not in employee_data or 'lastName' not in employee_data):
                full_name_parts = employee_data['fullName'].split(' ', 1)
                if len(full_name_parts) >= 2:
                    employee_data['firstName'] = full_name_parts[0]
                    employee_data['lastName'] = full_name_parts[1]
                elif len(full_name_parts) == 1:
                    employee_data['firstName'] = full_name_parts[0]
                    employee_data['lastName'] = full_name_parts[0]
            
            # Validate required fields
            required_fields = ['fullName', 'firstName', 'lastName']
            for field in required_fields:
                if not employee_data.get(field):
                    raise ValueError(f"Missing required field: {field}")
            
            # Update Jira - starting
            update_jira_ticket(
                ticket_key, 
                " Automated onboarding process started. Creating AD account..."
            )
            
            # Get AD credentials
            ad_creds = get_secret(AD_CREDENTIALS_SECRET)
            
            if not ad_creds:
                raise ValueError("AD credentials not found in Secrets Manager. Please ensure the secret exists and Lambda has access permissions.")
            
            # Create AD user (with access replication if specified)
            ad_result = create_ad_user(employee_data, ad_creds)
            
            # Update Jira with AD creation result
            update_jira_ticket(ticket_key, ad_result)
            
            # Trigger AD sync
            update_jira_ticket(
                ticket_key,
                " AD sync triggered. Microsoft 365 and Atlassian integration scheduled for 15 minutes to allow sync completion..."
            )
            
            sync_success = trigger_ad_sync(ad_result['domain'], ad_result['dc_host'])
            
            # Schedule M365 and Atlassian processing with delay
            try:
                copy_from_user = employee_data.get('copyAccessFrom') or employee_data.get('replicateAccessFrom')
                source_user_identifier = None
                
                if copy_from_user:
                    # Try to find source user's email from AD first
                    source_user_info = find_user_in_ad(copy_from_user, ad_result['domain'], ad_result['dc_host'])
                    if source_user_info and source_user_info.get('email'):
                        source_user_identifier = source_user_info['email']
                    else:
                        # Use the provided name as-is
                        source_user_identifier = copy_from_user
                
                # Schedule M365 and Atlassian processing with 15-minute delay
                m365_schedule_result = schedule_m365_processing(
                    ad_result['email'], 
                    ticket_key,
                    employee_data,
                    source_user_identifier,
                    delay_seconds=900  # 15 minutes
                )
                
                if m365_schedule_result.get('scheduled'):
                    success_message = f""" **AD Account Created Successfully!**

**Account Details:**
- Username: {ad_result.get('username')}
- Email: {ad_result.get('email')}
- Domain: {ad_result.get('domain')}
- OU: {ad_result.get('ou', 'N/A')}

**Microsoft 365 & Atlassian Integration:**
 Scheduled for {m365_schedule_result['delay_minutes']} minutes from now to allow AD sync completion.

**Next Steps:**
- AD Connect sync in progress
- M365 license assignment will be attempted automatically
- Atlassian account will be created (if enabled)
- You will receive another update when setup is complete"""
                    
                    if ad_result.get('access_replicated_from'):
                        replication_summary = ad_result.get('replication_summary', {})
                        success_message += f"""

**Access Replicated From:** {ad_result['access_replicated_from']}
- AD Groups Copied: {len(replication_summary.get('groups_copied', []))}
- M365 & Atlassian Access: Will be replicated after sync"""
                    
                    update_jira_ticket(ticket_key, success_message, success=True)
                else:
                    # Fallback to immediate processing
                    ad_result['m365_results'] = m365_schedule_result
                    update_jira_ticket(ticket_key, ad_result)
                
            except Exception as m365_error:
                error_msg = f"Integration scheduling failed: {str(m365_error)}"
                print(error_msg)
                
                update_jira_ticket(
                    ticket_key,
                    f" AD account created successfully, but M365/Atlassian scheduling encountered issues:\n\n{error_msg}\n\nPlease manually complete setup.",
                    success=True
                )
            
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'success': True,
                    'result': ad_result,
                    'm365_scheduled': True,
                    'atlassian_enabled': ATLASSIAN_ENABLED
                })
            }
            
        except Exception as e:
            error_msg = str(e)
            print(f"Error: {error_msg}")
            
            # Update Jira with error
            if ticket_key:
                update_jira_ticket(
                    ticket_key,
                    f" Onboarding automation failed: {error_msg}\n\nManual intervention required.",
                    success=False
                )
                
                # Send error notification
                send_error_notification(error_msg, ticket_key)
            
            return {
                'statusCode': 500,
                'body': json.dumps({
                    'success': False,
                    'error': error_msg
                })
            }
    
    else:
        return {
            'statusCode': 400,
            'body': json.dumps({
                'success': False,
                'error': 'Unknown event type'
            })
        }
