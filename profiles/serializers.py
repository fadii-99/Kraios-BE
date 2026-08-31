from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from accounts.models import User


class ProfileSerializer(serializers.ModelSerializer):
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
            'date_joined',
        )
        read_only_fields = ('id', 'email', 'role', 'date_joined')


class PasswordChangeRequestSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        user = self.context['request'].user

        if not user.check_password(attrs['current_password']):
            raise serializers.ValidationError(
                {'current_password': 'Current password is incorrect.'}
            )

        validate_password(attrs['new_password'], user=user)
        return attrs


class DeleteAccountRequestSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_current_password(self, password):
        user = self.context['request'].user

        if not user.check_password(password):
            raise serializers.ValidationError('Current password is incorrect.')

        return password


class OTPConfirmSerializer(serializers.Serializer):
    verification_id = serializers.UUIDField()
    otp = serializers.RegexField(
        regex=r'^\d{6}$',
        write_only=True,
        error_messages={'invalid': 'OTP must contain exactly 6 digits.'},
    )

