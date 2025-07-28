
#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <errno.h>
#include <libad2k.h>



static void waitResponse(unsigned char cmd, char *msg)
{
	if (msg != NULL && strlen(msg) > 0) {
		printf("%s\n", msg);

	}
	printf("waiting response...\n");
	static struct AD2kData ad2kData;
	while (1) {
		memset(&ad2kData, 0, sizeof(ad2kData));
		ad2k_FetchData(&ad2kData);
		if (ad2kData.type == AD2K_MSG_RESP
			&& ad2kData.data.response.cmd == cmd) {
			break;
		}
	}

	switch (ad2kData.data.response.result) {
	case RSP_RW_OK:
		printf("OK!\n");
		break;
	case RSP_RW_BCC_ERR:
		printf("Failed: BCC error\n");
		break;
	case RSP_RW_CMD_NOT_SPT:
		printf("Failed: Command is not supported\n");
		break;
	case RSP_W_TR_WRN:
		printf("Failed: TARE wrong response(SPEC forbid / out of range)\n");
		break;
	case RSP_W_RZ_WRN:
		printf("Failed: RE-ZERO wrong response(SPEC forbid / out of range)\n");
		break;
	case RSP_RW_SPAN_SW_OFF:
		printf("Failed: Span Switch is OFF\n");
		break;
	case RSP_W_OTHER:
		printf("Failed: Other response (SPAN/ZERO change)\n");
		break;
	case RSP_W_CHKSUM_ERR:
		printf("Failed: Validate Checksum error\n");
		break;
	default:
		printf("Unknown: %c'\n", ad2kData.data.response.result);
		break;
	}
}

static void showHelp()
{
	printf("%c - %s\n", CMD_W_CALB_ZERO, "Zero calibration");
	printf("%c - %s\n", CMD_W_CALB_SPAN, "Span calibration");
	printf("%c - %s\n", CMD_W_OTCH_TARE, "One touch tare");
	printf("%c - %s\n", CMD_W_DIGI_TARE, "Digital tare");
	printf("%c - %s\n", CMD_W_PRESET_TARE, "Prest tare");
	printf("%c - %s\n", CMD_W_PERCENT_TARE, "Percent tare");
	printf("%c - %s\n", CMD_W_ZERO_RST, "Zero reset");
	printf("%c - %s\n", CMD_W_INT_MODE, "Internal count mode");
	printf("%c - %s\n", CMD_W_WGT_MODE, "Weighing mode");
	printf("%c - %s\n", CMD_W_ZRST_PWR_ON, "Zero reset when power on");
	printf("%c - %s\n", CMD_W_VLD_CHKSUM, "Validate AD Box Driver Checksum");
	printf("%c - %s\n", CMD_W_SET_CHKSUM, "Write Driver Checksum");
	
	printf("get - get weight and tare\n");
	printf("quit/exit - quit\n");
	printf("?/help - show this help message\n");
}

static void getWeightTare()
{
	printf("getting data...\n");
	static struct AD2kData ad2kData;
	while (1) {
		memset(&ad2kData, 0, sizeof(ad2kData));
		ad2k_FetchData(&ad2kData);
		if (ad2kData.type == AD2K_MSG_NI) {
			break;
		}
	}
	printf("weight: %d, tare: %d\n", ad2kData.data.scaleData.weight, ad2kData.data.scaleData.tare);
}

static void processCmd(unsigned char *input)
{
	if (input == NULL) {
		return;
	}

	char *param = strchr(input, ' ');
	if (param != NULL) {
		param++;
	}

	char msg[128] = {0};
	int value;

	switch (input[0]) {
	case CMD_RW_SPEC_DATA:
		break;
		
	case CMD_RW_SPAN_DATA:
		break;
		
	case CMD_R_AD_VER:
		break;
		
	case CMD_W_CALB_ZERO:
		ad2k_CalbZero();
		break;
		
	case CMD_W_CALB_SPAN:
		ad2k_CalbSpan();
		break;
		
	case CMD_W_OTCH_TARE:
		ad2k_SetOneTouchTare();
		break;
		
	case CMD_W_DIGI_TARE:
		ad2k_SetDigitalTare(ad2k_StrToNum(param, BASE_DEC));
		break;
		
	case CMD_W_PRESET_TARE:
		ad2k_SetPresetTare(ad2k_StrToNum(param, BASE_DEC));
		break;
		
	case CMD_W_PERCENT_TARE:
		ad2k_SetPercentTare(ad2k_StrToNum(param, BASE_DEC));
		break;
		
	case CMD_W_ZERO_RST:
		ad2k_ZeroReset();
		break;
		
	case CMD_W_INT_MODE:
		ad2k_IRCntMode();
		break;
		
	case CMD_W_WGT_MODE:
		ad2k_WgtMode();
		break;
		
	case CMD_W_ZRST_PWR_ON:
		ad2k_ZeroResetPowerOn();
		break;
		
	case CMD_W_VLD_CHKSUM:
		value = ad2k_StrToNum(param, BASE_HEX);
		snprintf(msg, sizeof(msg), "validate checksum(HEX): %08X...", value);
		ad2k_ValidateChkSum(value);
		break;
		
	case CMD_W_SET_CHKSUM:
		value = ad2k_StrToNum(param, BASE_HEX);
		snprintf(msg, sizeof(msg), "set checksum(HEX): %08X...", value);
		ad2k_SetChkSum(value);
		break;
		
	default:
		printf("Invalid command: '%s'\n", input);
		return;
	}

	waitResponse(input[0], msg);
}


int main(int argc, char *argv[])
{
	if (argc < 2) {
		printf("usage: %s <port>\n", argv[0]);
		return -1;
	}

	struct ad2k_port portConf;

	strcpy(portConf.portName, argv[1]);
	portConf.parity = 'E';
	portConf.baudRate = 19200;
	portConf.dataBit = 8;
	portConf.stopBit = 1;

	printf("open port '%s'...", argv[1]);
	if (ad2k_OpenScale(&portConf) != 0) {
		printf("Failed\n");
		return -1;
	}
	printf("OK\n");

	printf("start scale...");
	if (ad2k_StartScale() != 0) {
		printf("Failed\n");
		ad2k_CloseScale();
		return -1;
	}
	printf("OK\n");

	unsigned char input[32];
	while (1) {
		printf("Input command: ");
		memset(input, 0, sizeof(input));
		fgets(input, sizeof(input), stdin);
		input[strlen(input) - 1] = '\0';
		if (strstr(input, "exit") || strstr(input, "quit")) {
			break;
		} else if (strstr(input, "?") || strstr(input, "help")) {
			showHelp();
		} else if (strstr(input, "get data")) {
			getWeightTare();
		} else {
			processCmd(input);
		}
	}

	ad2k_StopScale();
	ad2k_CloseScale();

	return 0;
}

