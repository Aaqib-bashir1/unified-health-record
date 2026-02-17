from django.core.mail import send_mail, EmailMultiAlternatives
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


def send_email(
    subject: str,
    message: str,
    recipient_list: list[str],
    html_message: str = None,
    fail_silently: bool = False
):
    """
    Generic email sender with HTML support.
    
    Args:
        subject: Email subject
        message: Plain text message body
        recipient_list: List of recipient email addresses
        html_message: Optional HTML version of the message
        fail_silently: If False, raise exception on failure
    
    Returns:
        int: Number of successfully sent emails
        
    Raises:
        Exception: If email sending fails and fail_silently=False
    """
    try:
        if html_message:
            # Use EmailMultiAlternatives for HTML emails
            email = EmailMultiAlternatives(
                subject=subject,
                body=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=recipient_list
            )
            email.attach_alternative(html_message, "text/html")
            result = email.send(fail_silently=fail_silently)
        else:
            # Simple text email
            result = send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=recipient_list,
                fail_silently=fail_silently,
            )
        
        logger.info(f"Email sent successfully to {recipient_list}")
        return result
        
    except Exception as e:
        logger.error(f"Failed to send email to {recipient_list}: {str(e)}")
        if not fail_silently:
            raise
        return 0


def send_bulk_email(subject: str, message: str, recipient_list: list[str]):
    """
    Send same email to multiple recipients efficiently.
    
    Args:
        subject: Email subject
        message: Email body
        recipient_list: List of recipient email addresses
    """
    from django.core.mail import send_mass_mail
    
    messages = [
        (subject, message, settings.DEFAULT_FROM_EMAIL, [recipient])
        for recipient in recipient_list
    ]
    
    try:
        send_mass_mail(messages, fail_silently=False)
        logger.info(f"Bulk email sent to {len(recipient_list)} recipients")
    except Exception as e:
        logger.error(f"Bulk email failed: {str(e)}")
        raise