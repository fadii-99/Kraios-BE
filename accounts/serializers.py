from django.db import transaction
from rest_framework import serializers

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
            print( 'An account with this email address already exists.')
            raise serializers.ValidationError(
                'An account with this email address already exists.'
            )

        return normalized_email

    def create(self, validated_data):
        with transaction.atomic():
            user = User.objects.create_user(
                email=validated_data['email'],
                password=None,
                full_name=validated_data['name'],
                firm_name=validated_data['firm'],
                country=validated_data['country'],
                is_active=False,
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
