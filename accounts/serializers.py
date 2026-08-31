from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers
from django.contrib.auth.hashers import make_password
from .models import SignupRequest, User


class SignupRequestSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    firm = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    country = serializers.CharField(max_length=100)
    date = serializers.DateField()
    time = serializers.CharField(max_length=100)

    def validate_email(self, email):
        normalized_email = email.lower()

        if User.objects.filter(email__iexact=normalized_email).exists():
            raise serializers.ValidationError(
                'An account with this email address already exists.'
            )

        return normalized_email

    def create(self, validated_data):
        with transaction.atomic():
            user = User.objects.create_user(
                email=validated_data['email'],
                password="123456789",
                full_name=validated_data['name'],
                firm_name=validated_data['firm'],
                country=validated_data['country'],
                is_active=True,
            )

            signup_request = SignupRequest.objects.create(
                user=user,
                preferred_date=validated_data['date'],
                preferred_time=validated_data['time'],
            )

        return signup_request


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, email):
        return email.lower()


class ForgotPasswordRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, email):
        return email.lower()


class ForgotPasswordConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    verification_id = serializers.UUIDField()
    otp = serializers.RegexField(
        regex=r'^\d{6}$',
        write_only=True,
        error_messages={'invalid': 'OTP must contain exactly 6 digits.'},
    )
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, email):
        return email.lower()

    def validate(self, attrs):
        user = self.context.get('user')
        if user is not None:
            validate_password(attrs['new_password'], user=user)
        return attrs


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            'id',
            'email',
            'full_name',
            'firm_name',
            'country',
            'job_title',
            'phone',
            'role',
            'is_active',
        )
        read_only_fields = fields
