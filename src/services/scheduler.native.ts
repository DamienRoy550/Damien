import type { ReminderScheduler } from '../tools/types';

/**
 * Real notification scheduler (expo-notifications). Loaded lazily so the
 * module (and its native side effects) never runs in Node tests.
 */
export async function getScheduler(): Promise<ReminderScheduler | undefined> {
  try {
    const Notifications = await import('expo-notifications');
    return {
      async requestPermissions() {
        const settings = await Notifications.getPermissionsAsync();
        let granted =
          settings.granted || settings.status === 'granted' || settings.ios?.status === 2;
        if (!granted) {
          const asked = await Notifications.requestPermissionsAsync();
          granted = asked.granted || asked.status === 'granted';
        }
        return Boolean(granted);
      },
      async schedule({ id, title, body, fireAt }) {
        Notifications.setNotificationHandler({
          handleNotification: async () => ({
            shouldShowAlert: true,
            shouldPlaySound: true,
            shouldSetBadge: false,
            // SDK 53+ fields (harmless if ignored on older hosts)
            shouldShowBanner: true,
            shouldShowList: true,
          }),
        });
        return Notifications.scheduleNotificationAsync({
          identifier: id,
          content: { title, body, sound: 'default' },
          trigger: { type: 'date', date: fireAt } as never,
        });
      },
    };
  } catch {
    return undefined;
  }
}
