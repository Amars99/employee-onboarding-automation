# Jira Automation Setup Guide

## Overview
This guide shows how to configure Jira Service Management to trigger your AWS Lambda for employee onboarding.

## Prerequisites
- Jira Service Management with Admin access
- AWS account with SNS topic deployed
- Custom fields created in Jira for employee data

## Step 1: Identify Your Custom Field IDs

### Finding Custom Field IDs
1. Go to **Jira Settings** → **Issues** → **Custom fields**
2. Find each field and click the **⚙️** (Configure)
3. Look at the URL - it will show `customfield_XXXXX`
4. Note down these IDs:

| Field Name | Custom Field ID | Purpose |
|------------|-----------------|---------|
| Full Name | customfield_11628 | Employee's complete name |
| Work Email | customfield_12519 | Company email address |
| Job Title | customfield_10062 | Position/role |
| Department | customfield_10061 | Team/department |
| Company | customfield_13014 | Company or subsidiary |
| Line Manager | customfield_13085 | Direct supervisor |
| Copy Access From | customfield_13082 | Template user for permissions |
| Work Location | customfield_XXXXX | Office location |
| Start Date | customfield_XXXXX | First day of work |

## Step 2: Create Jira Automation Rule

### Navigate to Automation
1. Go to **Project Settings** → **Automation**
2. Click **Create rule**

### Configure the Trigger
1. **When**: Issue transitioned
2. **From status**: Waiting for Support
3. **To status**: In Progress

### Add Conditions (Security)
1. **Add component** → **New condition** → **Issue fields condition**
   - Field: Project
   - Condition: equals
   - Value: Your IT Service Desk project (e.g., ITSD)

2. **Add component** → **New condition** → **Issue fields condition**
   - Field: Issue Type
   - Condition: equals  
   - Value: Employee Onboarding (or your request type name)

### Add SNS Action
1. **Add component** → **New action** → **Send message to Amazon SNS topic**

2. **Connect to AWS**:
   - Click **Connect**
   - Enter Topic ARN: `arn:aws:sns:eu-west-1:YOUR-ACCOUNT:employee-onboarding-prod-trigger`
   - Click **Save**

3. **Configure Message**:
   - Format: **Custom data**
   - Add these key-value pairs:

| Key | Value | Description |
|-----|-------|-------------|
| ticketKey | {{issue.key}} | Jira ticket ID |
| employeeData.fullName | {{issue.customfield_11628}} | Full name |
| employeeData.firstName | {{issue.customfield_11628.split(" ").first}} | First name |
| employeeData.lastName | {{issue.customfield_11628.split(" ").last}} | Last name |
| employeeData.email | {{issue.customfield_12519}} | Work email |
| employeeData.jobTitle | {{issue.customfield_10062}} | Job title |
| employeeData.department | {{issue.customfield_10061}} | Department |
| employeeData.company | {{issue.customfield_13014}} | Company |
| employeeData.manager | {{issue.customfield_13085}} | Manager name |
| employeeData.copyAccessFrom | {{issue.customfield_13082}} | Template user |
| employeeData.workLocation | {{issue.customfield_XXXXX}} | Location |
| employeeData.startDate | {{issue.customfield_XXXXX}} | Start date |

### Enable the Rule
1. **Name**: Employee Onboarding - Trigger AWS Automation
2. **Description**: Triggers Lambda when onboarding ticket moves to In Progress
3. Click **Turn on rule**

## Step 3: Test the Integration

### Create Test Ticket
1. Create new Employee Onboarding request
2. Fill in all required fields:
   - Full Name: Test User
   - Work Email: test.user@company.com
   - Department: IT
   - Job Title: Test Engineer
   - Manager: Your Name
   - Copy Access From: existing.user@company.com

### Trigger Automation
1. Move ticket from **Waiting for Support** → **In Progress**
2. Check ticket comments for Lambda updates
3. Monitor AWS CloudWatch logs

## Step 4: Production Checklist

### Before Going Live
- [ ] All custom field IDs mapped correctly
- [ ] SNS topic ARN is correct
- [ ] Automation rule conditions restrict to correct project/issue type
- [ ] Test with dummy data successful
- [ ] Lambda function deployed and tested
- [ ] Secrets configured in AWS Secrets Manager
- [ ] Cross-account roles configured (if using multiple AWS accounts)
- [ ] Error notifications configured

### Monitoring
- Check failed automations: **Project settings** → **Automation** → **Audit log**
- AWS CloudWatch: Monitor Lambda executions
- Jira ticket comments: Verify updates are posting

## Common Issues

### Automation Not Triggering
- Verify transition names match exactly
- Check project and issue type conditions
- Ensure user has permission to execute automation

### SNS Connection Failed
- Verify Topic ARN is correct
- Check AWS SNS topic policy allows Atlassian account (815843069303)
- Ensure no KMS encryption on SNS topic

### Missing Data in Lambda
- Verify custom field IDs are correct
- Check smart values syntax in automation
- Ensure fields have values before transition

## Smart Values Reference

### Useful Jira Smart Values
```
{{issue.key}}                  - Ticket ID (ITSD-123)
{{issue.summary}}               - Ticket title
{{issue.description}}           - Ticket description
{{issue.reporter.displayName}}  - Who created ticket
{{issue.reporter.emailAddress}} - Reporter's email
{{issue.assignee.displayName}}  - Assigned to
{{issue.created}}               - Creation date
{{issue.updated}}               - Last update
{{now}}                         - Current timestamp
{{issue.status.name}}           - Current status
{{transition.name}}             - What transition occurred
```

### String Manipulation
```
{{issue.customfield.split(" ").first}}  - First word
{{issue.customfield.split(" ").last}}   - Last word
{{issue.customfield.toLowerCase()}}      - Convert to lowercase
{{issue.customfield.replace("old","new")}} - Replace text
```

## Security Notes

1. **Never include** sensitive data like passwords in automation messages
2. **Always restrict** automation to specific projects/issue types
3. **Use conditions** to prevent automation from affecting wrong tickets
4. **Monitor usage** via audit logs regularly
5. **Test thoroughly** in a test project first
